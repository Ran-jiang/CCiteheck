"""北大法宝 MCP 客户端。

凭证从环境变量或本地 ``.env`` 文件读取，不写入 SQLite 或日志。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Optional

import httpx

from ....domain.legal_numbers import int_to_chinese_number
from ....infrastructure.database import normalize_article_key

from ....infrastructure.config import load_project_env
from ....infrastructure.http import default_ssl_context
from ....infrastructure.debug_timing import measure
from ....query_construction.strategies.common import build_article_semantic_fallback_query
from .models import (
    PkulawArticle,
    PkulawCaseRecord,
    PkulawLawRecord,
    PkulawMcpError,
    PkulawNotConfiguredError,
    PkulawNotFoundError,
    PkulawRecognizedLaw,
)
from .parsing import (
    parse_article_records as _parse_article_search_response,
    parse_case_records as _parse_case_list_response,
    parse_semantic_case_text as _parse_semantic_case_text,
    parse_exact_article as _parse_get_article_response,
    parse_law_records as _parse_law_list_response,
    parse_recognized_laws as _parse_law_recognition_response,
)


DEFAULT_GATEWAY = "https://apim-gateway.pkulaw.com"
MCP_ENDPOINTS = {
    "law_keyword": "/mcp-law",
    "law_semantic": "/mcp-law-search-service",
    "law_item": "/mcp-fatiao",
    "law_recognition": "/law_recognition",
    "case_keyword": "/mcp-case",
    "case_semantic": "/mcp-case-search-service",
}

_PKULAW_HTTP_CLIENT: httpx.Client | None = None
_PKULAW_HTTP_CLIENT_LOCK = threading.Lock()


def pkulaw_http_client() -> httpx.Client:
    """北大法宝专用直连连接池，不与 LLM 连接或 macOS 显式代理混用。"""
    global _PKULAW_HTTP_CLIENT
    if _PKULAW_HTTP_CLIENT is None:
        with _PKULAW_HTTP_CLIENT_LOCK:
            if _PKULAW_HTTP_CLIENT is None:
                _PKULAW_HTTP_CLIENT = httpx.Client(
                    verify=default_ssl_context(),
                    trust_env=False,
                    limits=httpx.Limits(
                        # 本机 TUN/法宝网关在并发 TLS 握手时会间歇超时；
                        # 单连接复用更稳定，且不会改变上层任务并行模型。
                        max_connections=1,
                        max_keepalive_connections=1,
                        keepalive_expiry=60.0,
                    ),
                )
    return _PKULAW_HTTP_CLIENT


class PkulawMcpClient:
    def __init__(
        self,
        access_token: Optional[str] = None,
        gateway: Optional[str] = None,
        timeout: int = 20,
    ):
        load_project_env()
        self.access_token = _clean_token(
            access_token or os.getenv("PKULAW_ACCESS_TOKEN")
        )
        self.gateway = (
            gateway or os.getenv("PKULAW_MCP_GATEWAY") or DEFAULT_GATEWAY
        ).rstrip("/")
        self.timeout = timeout
        self.connect_timeout = float(
            os.getenv("PKULAW_CONNECT_TIMEOUT_SECONDS", "15")
        )
        self.read_timeout = float(
            os.getenv("PKULAW_READ_TIMEOUT_SECONDS", str(max(timeout, 30)))
        )
        self._initial_probe = True
        self._probe_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._circuit_lock = threading.Lock()
        self._circuit_open_until = 0.0
        self._circuit_message = ""

    def get_article(self, title: str, article_no: str) -> PkulawArticle:
        normalized = normalize_article_no(article_no)
        payload = self._timed_call_tool(
            "retrieval.pkulaw_semantic",
            endpoint=MCP_ENDPOINTS["law_semantic"],
            tool_name="get_article",
            arguments={"title": title, "number": normalized},
        )
        data = _extract_payload_data(payload)
        return _parse_get_article_response(data, normalized)

    def get_law_item_content(self, title: str, article_no: str) -> PkulawArticle:
        number = normalize_article_key(article_no).replace("-", ".")
        if not re.fullmatch(r"\d+(?:\.\d+)?", number):
            raise PkulawMcpError(f"invalid article number: {article_no}")
        if "." in number:
            base, suffix = number.split(".", 1)
            if len(suffix) != 1:
                raise PkulawMcpError(
                    f"law_item cannot safely encode article suffix: {article_no}"
                )
            tool_number: int | float = float(f"{base}.{suffix}")
        else:
            tool_number = int(number)
        payload = self._timed_call_tool(
            "retrieval.pkulaw_law_item",
            endpoint=MCP_ENDPOINTS["law_item"],
            tool_name="get_law_item_content",
            arguments={"title": title, "tiao_num": tool_number},
        )
        return _parse_get_article_response(_extract_payload_data(payload), article_no)

    def get_law_list(
        self, title: str = "", fulltext: str = ""
    ) -> list[PkulawLawRecord]:
        if not title and not fulltext:
            raise PkulawMcpError("title or fulltext is required")
        payload = self._timed_call_tool(
            "retrieval.pkulaw_keyword",
            endpoint=MCP_ENDPOINTS["law_keyword"],
            tool_name="get_law_list",
            arguments={"title": title, "fulltext": fulltext},
        )
        data = _extract_payload_data(payload)
        return _parse_law_list_response(data)

    def search_law_articles(self, text: str) -> list[PkulawArticle]:
        if not text.strip():
            raise PkulawMcpError("semantic law query is required")
        payload = self._timed_call_tool(
            "retrieval.pkulaw_semantic",
            endpoint=MCP_ENDPOINTS["law_semantic"],
            tool_name="search_article",
            arguments={"text": text},
        )
        data = _extract_payload_data(payload)
        return _parse_article_search_response(data)

    def search_law_articles_for_article(
        self, title: str, article_no: str
    ) -> list[PkulawArticle]:
        return self.search_law_articles(
            build_article_semantic_fallback_query(title, article_no)
        )

    def recognize_laws(self, text: str) -> list[PkulawRecognizedLaw]:
        if not text.strip():
            raise PkulawMcpError("law recognition text is required")
        payload = self._timed_call_tool(
            "retrieval.pkulaw_recognition",
            endpoint=MCP_ENDPOINTS["law_recognition"],
            tool_name="law_recognition",
            arguments={"text": text},
        )
        return _parse_law_recognition_response(_extract_payload_data(payload))

    def get_case_list(
        self, title: str = "", fulltext: str = ""
    ) -> list[PkulawCaseRecord]:
        if not title and not fulltext:
            raise PkulawMcpError("case title or fulltext is required")
        payload = self._timed_call_tool(
            "retrieval.pkulaw_keyword",
            endpoint=MCP_ENDPOINTS["case_keyword"],
            tool_name="get_case_list",
            # 网关的 schema 是扁平且全小写的 title/fulltext。包成 ``caseInput``
            # 或写成首字母大写都不会报参数错，而是被当作未传值，于是返回
            # “标题关键词或正文关键词至少有一个不为空”，看起来像没给查询词。
            arguments={"title": title, "fulltext": fulltext},
        )
        data = _extract_payload_data(payload)
        return _parse_case_list_response(data)

    def search_cases(self, text: str) -> list[PkulawCaseRecord]:
        if not text.strip():
            raise PkulawMcpError("semantic case query is required")
        payload = self._timed_call_tool(
            "retrieval.pkulaw_semantic",
            endpoint=MCP_ENDPOINTS["case_semantic"],
            tool_name="search_case",
            arguments={"text": text},
        )
        data = _extract_payload_data(payload, allow_text=True)
        return (
            _parse_semantic_case_text(data)
            if isinstance(data, str)
            else _parse_case_list_response(data)
        )

    def _timed_call_tool(
        self,
        timing_name: str,
        endpoint: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        with measure(timing_name):
            return self._call_tool(endpoint, tool_name, arguments)

    def _call_tool(
        self, endpoint: str, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        if not self.access_token:
            raise PkulawNotConfiguredError("北大法宝未配置访问令牌，请先配置 PKULAW_ACCESS_TOKEN")

        # 一份文档会并发查询多条法规和案例。首次请求先做单飞探测；若鉴权、
        # 限流或网络故障，后续请求直接复用失败结果，避免同一故障打满上游。
        if self._initial_probe:
            with self._probe_lock:
                self._raise_if_circuit_open()
                if self._initial_probe:
                    try:
                        return self._serialized_tool_call(endpoint, tool_name, arguments)
                    finally:
                        self._initial_probe = False
        self._raise_if_circuit_open()
        return self._serialized_tool_call(endpoint, tool_name, arguments)

    def _serialized_tool_call(
        self, endpoint: str, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        """串行复用一条 TLS 连接，避免并发握手触发网关超时。"""
        with measure("retrieval.pkulaw_queue_wait"):
            self._request_lock.acquire()
        try:
            self._raise_if_circuit_open()
            return self._perform_tool_call(endpoint, tool_name, arguments)
        finally:
            self._request_lock.release()

    def _perform_tool_call(
        self, endpoint: str, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        url = f"{self.gateway}{endpoint}"
        request_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        }
        # 网络抖动/网关 5xx 退避重试；"未找到数据"是成功响应，不会走到这里。
        # 重试耗尽后抛 PkulawMcpError → 上游只报"无法判断"，绝不误判"法源不存在"。
        last_error: Exception | None = None
        last_failure_was_service_error = False
        for attempt in range(3):
            if attempt:
                time.sleep(0.5 * (2 ** (attempt - 1)))
            try:
                response = pkulaw_http_client().post(
                    url,
                    content=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
                    headers=headers,
                    timeout=httpx.Timeout(
                        connect=self.connect_timeout,
                        read=self.read_timeout,
                        write=min(float(self.timeout), 15.0),
                        pool=min(float(self.timeout), 10.0),
                    ),
                )
                if response.status_code >= 400:
                    last_error = PkulawMcpError(
                        _http_error_message(response.status_code, response.text)
                    )
                    if response.status_code < 500:  # 4xx 是配置/鉴权问题，重试无意义
                        if response.status_code in {401, 403, 429}:
                            self._open_circuit(
                                last_error,
                                300 if response.status_code in {401, 403} else 60,
                            )
                        raise last_error
                    last_failure_was_service_error = True
                    continue
                return _parse_mcp_response(response.text)
            except httpx.HTTPError as exc:
                last_failure_was_service_error = False
                last_error = PkulawMcpError(f"北大法宝网络请求失败：{exc}")
        if last_error is None:  # pragma: no cover - 循环固定至少执行一次
            last_error = PkulawMcpError("北大法宝数据源调用失败")
        # 连接/TLS 抖动只影响当前请求，不得连带阻断本次文档其余法规与案例。
        # 只有已确认的网关 5xx 才做短时熔断；鉴权与额度已在上面单独处理。
        if last_failure_was_service_error:
            self._open_circuit(last_error, 30)
        raise last_error

    def _raise_if_circuit_open(self) -> None:
        with self._circuit_lock:
            if self._circuit_open_until <= time.monotonic():
                self._circuit_open_until = 0.0
                self._circuit_message = ""
                return
            raise PkulawMcpError(f"{self._circuit_message}；本次核查已暂停重复请求")

    def _open_circuit(self, error: PkulawMcpError, cooldown: int) -> None:
        with self._circuit_lock:
            self._circuit_message = str(error)
            self._circuit_open_until = time.monotonic() + cooldown


_ARTICLE_NO = re.compile(r"^第(?P<base>\d+)条(?:之(?P<suffix>\d+))?$")


def normalize_article_no(article_no: str) -> str:
    text = article_no.strip()
    match = _ARTICLE_NO.fullmatch(text)
    if not match:
        return text
    try:
        base = int_to_chinese_number(int(match.group("base")))
        suffix = match.group("suffix")
        return f"第{base}条" + (
            f"之{int_to_chinese_number(int(suffix))}" if suffix else ""
        )
    except ValueError:
        return text


def _parse_mcp_response(raw: str) -> Any:
    text = raw.strip()
    if not text:
        raise PkulawMcpError("Pkulaw MCP returned an empty response")
    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            item = line.removeprefix("data:").strip()
            if item and item != "[DONE]":
                events.append(json.loads(item))
    if events:
        return events[-1]
    return json.loads(text)


def _extract_payload_data(payload: Any, *, allow_text: bool = False) -> Any:
    if isinstance(payload, dict) and "error" in payload:
        raise PkulawMcpError(str(payload["error"]))
    result = payload.get("result") if isinstance(payload, dict) else payload
    content = result.get("content") if isinstance(result, dict) else None
    is_error = bool(isinstance(result, dict) and result.get("isError"))
    if isinstance(content, list) and content:
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text", "")
            if not text:
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # 法宝工具在未命中时会返回纯文本错误（如 Data 为 None 的
                # pydantic 校验错误），归类为"检索完成但未找到"
                if is_error and ("input_value=None" in text or "未找到" in text):
                    raise PkulawNotFoundError("未找到数据") from None
                if allow_text:
                    return text
                raise PkulawMcpError(text[:300]) from None
    if is_error:
        raise PkulawMcpError("Pkulaw tool returned an error without detail")
    return result


def _clean_token(token: Optional[str]) -> Optional[str]:
    if token is None:
        return None
    value = token.strip().strip('"').strip("'")
    return value[7:].strip() if value.lower().startswith("bearer ") else value


def _http_error_message(status: int, detail: str) -> str:
    if status in {401, 403}:
        return f"北大法宝鉴权失败（HTTP {status}），请检查访问令牌、账户状态或剩余点数"
    if status == 429:
        return "北大法宝请求额度受限（HTTP 429），请稍后重试或检查账户剩余点数"
    if status >= 500:
        return f"北大法宝服务暂时不可用（HTTP {status}）"
    clean_detail = " ".join(detail.split())[:160]
    suffix = f"：{clean_detail}" if clean_detail else ""
    return f"北大法宝请求被拒绝（HTTP {status}）{suffix}"
