"""Ansvar Gateway MCP 客户端。

对接标准 Streamable HTTP MCP 服务（https://gateway.ansvar.eu/mcp）：
先 initialize 握手（按协议版本协商，兼容服务端不分配会话 ID 的无状态
模式），再读 tools/list 发现可用工具，按需调用。与 EUR-Lex 客户端
不同：不强制要求会话 ID（MCP 标准允许服务端不分配），工具名不硬编码
而靠 tools/list 发现，并区分额度超限（-32000/cap_exceeded）与套餐不可用
（-32601）等工具级错误。

后端保管凭证（Bearer 令牌只在 Authorization 头发送），只把检索所需的
法名、条号和必要查询信息发给服务端。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from ....infrastructure.config import load_project_env
from ....infrastructure.http import BROWSER_USER_AGENT, default_ssl_context
from .auth import AnsvarAuthError, AnsvarAuthExpired, AnsvarOAuth


class AnsvarMcpError(RuntimeError):
    """Ansvar MCP 调用失败（网络、网关或响应格式问题）。"""


class AnsvarNotConfiguredError(AnsvarMcpError):
    """Ansvar 网关未配置。"""


class AnsvarQuotaExceededError(AnsvarMcpError):
    """额度超限（-32000 / cap_exceeded）。非 HTTP 429。"""


class AnsvarPackageUnavailableError(AnsvarMcpError):
    """套餐不可用（-32601）。"""


@dataclass(frozen=True)
class AnsvarRecord:
    """一条 Ansvar 检索命中。"""

    title: str
    identifier: str = ""
    url: str = ""
    jurisdiction: str = ""
    in_force: Optional[bool] = None
    version_label: str = ""
    snippet: str = ""
    publisher: str = ""
    license: str = ""
    citation: dict[str, Any] = field(default_factory=dict)
    lookup_arguments: dict[str, Any] = field(default_factory=dict)


_PROTOCOL_VERSION = "2025-06-18"


class AnsvarMcpClient:
    def __init__(
        self,
        gateway: Optional[str] = None,
        access_token: Optional[str] = None,
        search_tool: Optional[str] = None,
        article_tool: Optional[str] = None,
        timeout: int = 30,
        oauth: Optional[AnsvarOAuth] = None,
    ):
        load_project_env()
        configured_gateway = gateway if gateway is not None else os.getenv("ANSVAR_MCP_GATEWAY")
        self.gateway = (configured_gateway or "").rstrip("/")
        if not self.gateway:
            raise AnsvarNotConfiguredError(
                "Ansvar 数据源未配置：缺少 ANSVAR_MCP_GATEWAY"
            )
        # ANSVAR_ACCESS_TOKEN 是短期令牌覆盖；未设则走服务凭证或 OAuth PKCE。
        self.access_token = access_token or os.getenv("ANSVAR_ACCESS_TOKEN") or ""
        self._oauth = oauth
        self._search_tool = search_tool or os.getenv("ANSVAR_SEARCH_TOOL")
        self._article_tool = article_tool or os.getenv("ANSVAR_ARTICLE_TOOL")
        self._search_schema: dict[str, Any] = {}
        self._article_schema: dict[str, Any] = {}
        self.timeout = timeout
        self._session_id: Optional[str] = None
        self._initialized = False
        self._tools: list[dict[str, Any]] = []
        self._auth_lock = threading.Lock()
        self._session_lock = threading.Lock()

    def _bearer(self) -> Optional[str]:
        """优先手动 token；否则用 OAuth 取（自动刷新）。"""
        if self.access_token:
            return self.access_token
        with self._auth_lock:
            if self._oauth is None:
                self._oauth = AnsvarOAuth()
            try:
                return self._oauth.get_bearer()
            except AnsvarAuthExpired:
                # refresh 失效 → 重新完整授权一次
                self._oauth.invalidate()
                return self._oauth.get_bearer()

    # ---- 公共检索接口 ----

    def search_law(
        self, query: str, jurisdiction: str = ""
    ) -> list[AnsvarRecord]:
        """按法名（+法域）检索候选法规。免费层每次只能一个法域或框架。"""
        if not query.strip():
            raise AnsvarMcpError("ansvar search query is required")
        tool, schema = self._resolve_search_tool()
        # 按工具 inputSchema 构造参数：query 必带，jurisdictions 单法域列表
        values: dict[str, Any] = {"query": query, "limit": 10}
        if jurisdiction:
            values["jurisdictions"] = [jurisdiction]
        arguments = _map_args_to_schema(schema, values)
        payload = self._call_tool(tool, arguments)
        try:
            return _parse_search_response(payload)
        except AnsvarMcpError as exc:
            if _is_not_found_error(str(exc)):
                return []
            raise

    def get_article_text(
        self,
        identifier: str,
        article_number: str,
        *,
        jurisdiction: str = "",
        lookup_arguments: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """按法律标识 + 条号取回条文原文；未命中该条返回 None。"""
        tool, schema = self._resolve_article_tool()
        values: dict[str, Any] = {
            "identifier": identifier,
            "law": identifier,
            "article": article_number,
            "jurisdiction": jurisdiction,
        }
        arguments = _filter_args_to_schema(schema, lookup_arguments or {})
        arguments.update({
            key: value
            for key, value in _map_args_to_schema(schema, values).items()
            if key not in arguments
        })
        try:
            payload = self._call_tool(tool, arguments)
            data = _tool_data(payload)
        except AnsvarMcpError as exc:
            if type(exc) is AnsvarMcpError and _is_not_found_error(str(exc)):
                return None
            raise
        if not isinstance(data, dict):
            return None
        # get_provision 实际返回 {"results": [{text, citation, ...}]} 信封，
        # citation 块带来源 URL/发布者/许可等溯源字段。
        results = data.get("results")
        if isinstance(results, list) and results and isinstance(results[0], dict):
            first = results[0]
            raw_citation = first.get("_citation") or first.get("citation") or {}
            citation = raw_citation if isinstance(raw_citation, dict) else {}
            content = str(first.get("text") or "").strip()
            if not content:
                return None
            in_force = first.get("in_force", first.get("inForce"))
            return {
                "text": content,
                "title": str(
                    citation.get("source_full_name") or citation.get("source") or ""
                ),
                "in_force": in_force if isinstance(in_force, bool) else None,
                "version_label": str(first.get("version_label") or first.get("version") or ""),
                "url": str(citation.get("source_url") or ""),
                "publisher": str(citation.get("publisher") or ""),
                "license": str(citation.get("license") or ""),
                "citation": dict(citation),
                "last_verified": str(citation.get("last_verified") or ""),
                "resolved_canonical_ref": str(citation.get("resolved_canonical_ref") or ""),
                "resolution_method": str(citation.get("resolution_method") or ""),
                # 辅助译文：仅用于跨语言语义比对，不替代原文作为直接引用依据
                "translation": str(first.get("translation") or first.get("translated_text") or ""),
                "version_date": str(
                    first.get("version_date") or first.get("effective_date")
                    or citation.get("version_date") or ""
                ),
            }
        provision = data.get("provision") if isinstance(data.get("provision"), dict) else {}
        raw_citation = data.get("_citation") or data.get("citation") or {}
        citation = raw_citation if isinstance(raw_citation, dict) else {}
        content = str(
            data.get("content")
            or data.get("text")
            or data.get("provision_text")
            or provision.get("text")
            or ""
        ).strip()
        if not content:
            return None
        in_force = data.get("in_force", data.get("inForce"))
        return {
            "text": content,
            "title": str(data.get("title") or ""),
            "in_force": in_force if isinstance(in_force, bool) else None,
            "version_label": str(data.get("version_label") or data.get("version") or ""),
            "url": str(data.get("url") or data.get("source_url") or citation.get("source_url") or ""),
            "publisher": str(data.get("publisher") or data.get("source") or citation.get("publisher") or ""),
            "license": str(data.get("license") or data.get("licence") or citation.get("license") or ""),
            "citation": dict(citation),
            "last_verified": str(citation.get("last_verified") or ""),
            "resolved_canonical_ref": str(citation.get("resolved_canonical_ref") or ""),
            "resolution_method": str(citation.get("resolution_method") or ""),
            # 辅助译文：仅用于跨语言语义比对，不替代原文作为直接引用依据
            "translation": str(data.get("translation") or data.get("translated_text") or ""),
            "version_date": str(
                data.get("version_date") or data.get("effective_date")
                or citation.get("version_date") or ""
            ),
        }

    # ---- 工具发现（tools/list）----

    def list_tools(self) -> list[dict[str, Any]]:
        """读取服务端 tools/list，校验可用工具，不照搬旧仓库接口。"""
        if self._tools:
            return self._tools
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }
        payload = self._post(body, session=self._ensure_session())
        data = _tool_data(payload)
        tools = []
        if isinstance(data, dict):
            tools = list(data.get("tools") or [])
        elif isinstance(data, list):
            tools = data
        self._tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        return self._tools

    # ---- MCP 会话与传输 ----

    def _resolve_search_tool(self) -> tuple[str, dict]:
        if self._search_tool:
            return self._search_tool, self._search_schema
        for tool in self.list_tools():
            name = str(tool.get("name") or "")
            if name == "search":
                self._search_tool = name
                self._search_schema = _input_schema(tool)
                return name, self._search_schema
        raise AnsvarMcpError(
            "Ansvar tools/list 中未发现检索工具，请用 ANSVAR_SEARCH_TOOL 显式配置"
        )

    def _resolve_article_tool(self) -> tuple[str, dict]:
        if self._article_tool:
            return self._article_tool, self._article_schema
        for tool in self.list_tools():
            name = str(tool.get("name") or "")
            if name == "get_provision":
                self._article_tool = name
                self._article_schema = _input_schema(tool)
                return name, self._article_schema
        raise AnsvarMcpError(
            "Ansvar tools/list 中未发现条文取回工具，请用 ANSVAR_ARTICLE_TOOL 显式配置"
        )

    def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        try:
            return self._post(body, session=self._ensure_session())
        except AnsvarMcpError as exc:
            # 会话过期（服务重启等）时重建一次；无状态服务无会话，直接重试
            if "session" not in str(exc).lower() and "404" not in str(exc):
                raise
            self._session_id = None
            self._initialized = False
            return self._post(body, session=self._ensure_session())

    def _ensure_session(self) -> Optional[str]:
        """握手；服务端可能不分配会话 ID（无状态模式），此时返回 None。

        与 EUR-Lex 客户端不同：不强制要求会话 ID，符合 MCP 传输规范。
        """
        if self._initialized:
            return self._session_id
        with self._session_lock:
            if self._initialized:
                return self._session_id
            init_body = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "ccitecheck", "version": "1.0"},
                },
            }
            _, session_id = self._post_raw(init_body, session=None)
            # 服务端可能不分配会话 ID —— 无状态模式，正常继续
            self._session_id = session_id
            self._post_raw(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                session=session_id,
            )
            self._initialized = True
            return session_id

    def _post(self, body: dict[str, Any], session: Optional[str]) -> Any:
        payload, _ = self._post_raw(body, session)
        if payload is None:
            raise AnsvarMcpError("Ansvar MCP returned an empty response")
        return payload

    def _post_raw(
        self, body: dict[str, Any], session: Optional[str], *, _retried_auth: bool = False,
    ) -> tuple[Any, Optional[str]]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _PROTOCOL_VERSION,
            "User-Agent": BROWSER_USER_AGENT,
        }
        if session:
            headers["Mcp-Session-Id"] = session
        # 后端保管凭证：bearer 由 OAuth 自动管理，令牌只在 Authorization 头出现
        try:
            bearer = self._bearer()
        except AnsvarAuthError as exc:
            raise AnsvarMcpError(f"Ansvar 认证失败：{exc}") from exc
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(
            self.gateway,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(3):
            if attempt:
                time.sleep(0.5 * (2 ** (attempt - 1)))
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout, context=default_ssl_context()
                ) as response:
                    session_id = response.headers.get("Mcp-Session-Id")
                    raw = response.read().decode("utf-8")
                return _parse_mcp_response(raw), session_id
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                # 401：token 过期/失效 → 重授权一次重试（静态 token 不重试）
                if exc.code == 401 and not _retried_auth and self._oauth is not None and not self.access_token:
                    self._oauth.expire_access_token()
                    return self._post_raw(body, session, _retried_auth=True)
                last_error = AnsvarMcpError(
                    f"Ansvar MCP HTTP {exc.code}: {detail[:300]}"
                )
                if exc.code < 500:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = AnsvarMcpError(f"Ansvar MCP request failed: {reason}")
        raise last_error


def _is_not_found_error(message: str) -> bool:
    lowered = message.lower()
    return "no results" in lowered or "not found" in lowered or "未找到" in lowered


def _parse_mcp_response(raw: str) -> Any:
    """解析 JSON-RPC 响应或 text/event-stream。"""
    text = raw.strip()
    if not text:
        return None
    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            item = line.removeprefix("data:").strip()
            if item and item != "[DONE]":
                events.append(json.loads(item))
    if events:
        return events[-1]
    return json.loads(text)


def _input_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """取工具 inputSchema；缺失时返回空 dict。"""
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    return schema if isinstance(schema, dict) else {}


def _map_args_to_schema(schema: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """把已知值按工具 inputSchema 的属性名对齐，只发 schema 声明的字段。

    容错单复数（jurisdiction/jurisdictions）与常见别名（query/q、
    identifier/source/citation）。未在 schema 中出现的字段丢弃，避免
    向严格校验的工具发未知参数。
    """
    if not schema:
        return dict(values)
    props = schema.get("properties") or {}
    if not isinstance(props, dict) or not props:
        return dict(values)
    prop_names = {p.lower(): p for p in props}
    aliases = {
        "query": ("query", "q", "keyword", "search"),
        "jurisdictions": ("jurisdictions", "jurisdiction", "country", "countries"),
        "jurisdiction": ("jurisdiction", "jurisdictions", "country", "countries"),
        "frameworks": ("frameworks", "framework"),
        "limit": ("limit", "max_results", "max"),
        "identifier": ("canonical_ref", "law", "identifier", "source", "citation", "law_id", "id"),
        "law": ("law", "identifier", "law_id"),
        "article": ("article", "article_number", "article_no", "provision", "section"),
    }
    mapped: dict[str, Any] = {}
    for key, val in values.items():
        if val in (None, "", []):
            continue
        candidates = aliases.get(key, (key,))
        real = next((prop_names[c] for c in candidates if c in prop_names), None)
        if real:
            target_type = (props.get(real) or {}).get("type")
            if target_type == "array" and not isinstance(val, list):
                val = [val]
            elif target_type == "string" and isinstance(val, list) and len(val) == 1:
                val = val[0]
            mapped[real] = val
    return mapped


def _filter_args_to_schema(
    schema: dict[str, Any], arguments: dict[str, Any]
) -> dict[str, Any]:
    """保留服务端 citation lookup 返回且仍在当前工具 schema 中的参数。"""
    if not schema:
        return dict(arguments)
    properties = schema.get("properties") or {}
    return {key: value for key, value in arguments.items() if key in properties}


def _classify_jsonrpc_error(error: dict[str, Any]) -> AnsvarMcpError:
    """区分额度超限、套餐不可用与普通错误。"""
    code = error.get("code")
    data = error.get("data")
    message = str(error.get("message") or data or "")
    machine_code = str(data.get("cause") or data.get("code") or "") if isinstance(data, dict) else ""
    lowered = f"{message} {machine_code}".lower()
    # 额度超限可能是 -32000 或 data.cap_exceeded，并非 HTTP 429
    if code == -32000 and ("cap_exceeded" in lowered or "quota" in lowered):
        return AnsvarQuotaExceededError(f"Ansvar 额度超限：{message[:200]}")
    # 套餐不可用可能表现为 -32601
    if code == -32601 or "package" in lowered or "tier" in lowered:
        return AnsvarPackageUnavailableError(f"Ansvar 套餐不可用：{message[:200]}")
    return AnsvarMcpError(f"Ansvar MCP error: {error}")


def _loads_json_prefix(text: str):
    """解析工具文本内容开头的 JSON 值（容忍尾随的 markdown 来源附注）。"""
    try:
        data, _ = json.JSONDecoder().raw_decode(text.lstrip())
        return data
    except ValueError:
        return None


def _tool_data(payload: Any) -> Any:
    """展开 JSON-RPC → tool result → structuredContent/文本 JSON。"""
    if isinstance(payload, dict) and "error" in payload:
        raise _classify_jsonrpc_error(payload["error"])
    data = payload
    if isinstance(payload, dict):
        data = payload.get("result", payload)
    if isinstance(data, dict):
        if data.get("isError"):
            text = ""
            for item in data.get("content") or []:
                if isinstance(item, dict) and item.get("text"):
                    text = item["text"]
                    break
            lowered = text.lower()
            if "cap_exceeded" in lowered or "quota" in lowered:
                raise AnsvarQuotaExceededError(f"Ansvar 工具级额度超限：{text[:200]}")
            raise AnsvarMcpError(f"Ansvar MCP tool error: {text[:300]}")
        if isinstance(data.get("structuredContent"), dict):
            return data["structuredContent"]
        if isinstance(data.get("content"), list):
            for item in data["content"]:
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                    parsed = _loads_json_prefix(item["text"])
                    return parsed if parsed is not None else {}
    return data


def _parse_search_response(payload: Any) -> list[AnsvarRecord]:
    """从 MCP 响应中提取结果列表；兼容不同实现的字段名，并提取 _citation provenance。"""
    data = _tool_data(payload)
    if isinstance(data, dict):
        for key in ("laws", "results", "items", "documents", "hits"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return []
    records = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        # 新版 Gateway 的法律元数据行把法名放在 text，其余身份放在 citation。
        citation = entry.get("_citation") or entry.get("citation") or {}
        if not isinstance(citation, dict):
            citation = {}
        is_law_metadata = str(citation.get("article") or "") == "meta"
        title = str(
            entry.get("title")
            or entry.get("law_title")
            or entry.get("name")
            or entry.get("label")
            or (str(entry.get("text") or "").split(" (", 1)[0] if is_law_metadata else "")
            or ""
        ).strip()
        if not title:
            continue
        in_force = entry.get("in_force", entry.get("inForce"))
        identifier = str(
            entry.get("identifier")
            or entry.get("id")
            or entry.get("law_id")
            or entry.get("canonical_ref")
            or entry.get("celex")
            or (citation.get("source") if is_law_metadata else "")
            or ""
        )
        url = str(
            entry.get("url") or entry.get("uri") or entry.get("source_url")
            or citation.get("source_url") or citation.get("url") or ""
        )
        jurisdiction = str(
            entry.get("jurisdiction") or entry.get("country")
            or citation.get("jurisdiction") or ""
        )
        version_label = str(entry.get("version_label") or entry.get("version") or "")
        lookup = citation.get("lookup") or {}
        lookup_arguments = (
            lookup.get("arguments") or lookup.get("args") or {}
            if isinstance(lookup, dict) else {}
        )
        if not isinstance(lookup_arguments, dict):
            lookup_arguments = {}
        if is_law_metadata:
            lookup_arguments = {}
        records.append(
            AnsvarRecord(
                title=title,
                identifier=identifier,
                url=url,
                jurisdiction=jurisdiction,
                in_force=in_force if isinstance(in_force, bool) else None,
                version_label=version_label,
                snippet=str(
                    entry.get("snippet")
                    or entry.get("provision_text")
                    or entry.get("text")
                    or ""
                )[:500],
                publisher=str(citation.get("publisher") or entry.get("publisher") or ""),
                license=str(citation.get("license") or entry.get("licence") or ""),
                citation=dict(citation),
                lookup_arguments=lookup_arguments,
            )
        )
    return records


__all__ = [
    "AnsvarMcpClient",
    "AnsvarMcpError",
    "AnsvarNotConfiguredError",
    "AnsvarQuotaExceededError",
    "AnsvarPackageUnavailableError",
    "AnsvarRecord",
]
