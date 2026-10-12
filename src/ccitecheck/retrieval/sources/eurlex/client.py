"""EUR-Lex MCP 网关客户端。

对接标准 Streamable HTTP MCP 服务（如 @cyanheads/eur-lex-mcp-server）：
先 initialize 握手取得会话，再按会话调用工具；会话失效自动重建。
"未找到"是成功响应；网络失败显式报错，绝不静默降级。未配置网关时抛
EurLexNotConfiguredError，由上游归入 source_not_configured 状态。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from ....infrastructure.config import load_project_env
from ....infrastructure.http import default_ssl_context


class EurLexMcpError(RuntimeError):
    """EUR-Lex MCP 调用失败（网络、网关或响应格式问题）。"""


class EurLexNotConfiguredError(EurLexMcpError):
    """EUR-Lex 网关未配置。"""


@dataclass(frozen=True)
class EurLexRecord:
    """一条 EUR-Lex 检索命中。"""

    title: str
    celex: str = ""
    url: str = ""
    in_force: Optional[bool] = None
    snippet: str = ""


DEFAULT_SEARCH_TOOL = "eurlex_search_documents"
DEFAULT_LOOKUP_TOOL = "eurlex_lookup_celex"
DEFAULT_DOCUMENT_TOOL = "eurlex_get_document"
_PROTOCOL_VERSION = "2025-06-18"


class EurLexMcpClient:
    def __init__(
        self,
        gateway: Optional[str] = None,
        access_token: Optional[str] = None,
        search_tool: Optional[str] = None,
        timeout: int = 30,
    ):
        load_project_env()
        self.gateway = (gateway or os.getenv("EURLEX_MCP_GATEWAY") or "").rstrip("/")
        self.access_token = access_token or os.getenv("EURLEX_ACCESS_TOKEN") or ""
        self.search_tool = (
            search_tool or os.getenv("EURLEX_SEARCH_TOOL") or DEFAULT_SEARCH_TOOL
        )
        self.timeout = timeout
        self._session_id: Optional[str] = None
        if not self.gateway:
            raise EurLexNotConfiguredError("欧盟法规数据源未配置：缺少 EURLEX_MCP_GATEWAY")

    def search_law(self, query: str, celex: str = "") -> list[EurLexRecord]:
        """检索欧盟法规。已知 CELEX 号时优先精确解析，其余走关键词检索。"""
        if celex:
            record = self._lookup_celex(celex, fallback_title=query)
            if record is not None:
                return [record]
        if not query.strip():
            raise EurLexMcpError("eurlex search query is required")
        payload = self._call_tool(self.search_tool, {"keyword": query, "limit": 5})
        try:
            return _parse_search_response(payload)
        except EurLexMcpError as exc:
            # 服务端把"零命中"包装成工具错误；对核查而言这是"未找到"，不是故障
            if _is_not_found_error(str(exc)):
                return []
            raise

    def get_article_text(
        self, celex: str, article_number: int
    ) -> Optional[dict[str, Any]]:
        """取回指定条文的英文原文（markdown）；未命中该条时返回 None。"""
        payload = self._call_tool(
            DEFAULT_DOCUMENT_TOOL,
            {
                "celex_number": celex,
                "select": {"articles": str(article_number)},
                "format": "markdown",
            },
        )
        data = _tool_data(payload)
        if not isinstance(data, dict):
            return None
        content = str(data.get("content") or "").strip()
        matched = (data.get("selection") or {}).get("matched") or []
        if not content or not matched:
            return None
        in_force = data.get("in_force")
        return {
            "text": content,
            "title": str(data.get("title") or ""),
            "in_force": in_force if isinstance(in_force, bool) else None,
        }

    def _lookup_celex(
        self, celex: str, fallback_title: str
    ) -> Optional[EurLexRecord]:
        payload = self._call_tool(DEFAULT_LOOKUP_TOOL, {"identifier": celex})
        data = _tool_data(payload)
        if not isinstance(data, dict) or not data.get("found"):
            return None
        resolved = str(data.get("celex_number") or celex)
        return EurLexRecord(
            title=fallback_title,
            celex=resolved,
            url=f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{resolved}",
        )

    # ---- MCP 会话与传输 ----

    def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        try:
            return self._post(body, session=self._ensure_session())
        except EurLexMcpError as exc:
            # 会话过期（服务重启等）时重建一次
            if "session" not in str(exc).lower() and "404" not in str(exc):
                raise
            self._session_id = None
            return self._post(body, session=self._ensure_session())

    def _ensure_session(self) -> str:
        if self._session_id:
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
        if not session_id:
            raise EurLexMcpError("EUR-Lex MCP did not return a session id")
        self._session_id = session_id
        self._post_raw(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session=session_id,
        )
        return session_id

    def _post(self, body: dict[str, Any], session: Optional[str]) -> Any:
        payload, _ = self._post_raw(body, session)
        if payload is None:
            raise EurLexMcpError("EUR-Lex MCP returned an empty response")
        return payload

    def _post_raw(
        self, body: dict[str, Any], session: Optional[str]
    ) -> tuple[Any, Optional[str]]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _PROTOCOL_VERSION,
        }
        if session:
            headers["Mcp-Session-Id"] = session
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
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
                last_error = EurLexMcpError(
                    f"EUR-Lex MCP HTTP {exc.code}: {detail[:300]}"
                )
                if exc.code < 500:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = EurLexMcpError(f"EUR-Lex MCP request failed: {reason}")
        raise last_error


def _is_not_found_error(message: str) -> bool:
    lowered = message.lower()
    return "no documents matched" in lowered or "not found" in lowered


def _parse_mcp_response(raw: str) -> Any:
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


def _tool_data(payload: Any) -> Any:
    """展开 JSON-RPC → tool result → structuredContent/文本 JSON。"""
    if isinstance(payload, dict) and "error" in payload:
        raise EurLexMcpError(f"EUR-Lex MCP error: {payload['error']}")
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
            raise EurLexMcpError(f"EUR-Lex MCP tool error: {text[:300]}")
        if isinstance(data.get("structuredContent"), dict):
            return data["structuredContent"]
        if isinstance(data.get("content"), list):
            for item in data["content"]:
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                    try:
                        return json.loads(item["text"])
                    except (json.JSONDecodeError, TypeError):
                        return {}
    return data


def _parse_search_response(payload: Any) -> list[EurLexRecord]:
    """从 MCP 响应中提取结果列表；兼容不同实现的字段名。"""
    data = _tool_data(payload)
    if isinstance(data, dict):
        for key in ("documents", "results", "items", "hits"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return []
    records = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        title = str(
            entry.get("title") or entry.get("name") or entry.get("label") or ""
        ).strip()
        if not title:
            continue
        in_force = entry.get("in_force", entry.get("inForce"))
        celex = str(entry.get("celex") or entry.get("celex_number") or "")
        url = str(entry.get("url") or entry.get("uri") or entry.get("work_uri") or "")
        if celex and (not url or "cellar" in url):
            url = f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"
        records.append(
            EurLexRecord(
                title=title,
                celex=celex,
                url=url,
                in_force=in_force if isinstance(in_force, bool) else None,
                snippet=str(entry.get("snippet") or entry.get("text") or "")[:500],
            )
        )
    return records


__all__ = [
    "EurLexMcpClient",
    "EurLexMcpError",
    "EurLexNotConfiguredError",
    "EurLexRecord",
]
