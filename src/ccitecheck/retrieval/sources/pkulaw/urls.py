"""北大法宝页面链接的兼容与质量判断。"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, urlparse


_MARKDOWN_URL = re.compile(r"\((https?://[^)]+)\)")


def plain_url(value: str | None) -> Optional[str]:
    """把 MCP 的 Markdown 链接归一为纯 URL。"""
    if not value:
        return None
    match = _MARKDOWN_URL.search(str(value))
    return match.group(1) if match else str(value) if str(value).startswith("http") else None


def is_legacy_mcp_url(value: str | None) -> bool:
    """识别法规列表接口返回、网页端已无法解析的旧 lar 链接。"""
    url = plain_url(value)
    if not url:
        return False
    parsed = urlparse(url)
    return (
        parsed.hostname in {"pkulaw.com", "www.pkulaw.com"}
        and parsed.path.startswith("/lar/")
        and "mcp" in parse_qs(parsed.query).get("way", [])
    )


def usable_mcp_url(value: str | None) -> Optional[str]:
    """只返回可供用户点击的 MCP 页面链接。"""
    return None if is_legacy_mcp_url(value) else plain_url(value)


__all__ = ["is_legacy_mcp_url", "plain_url", "usable_mcp_url"]
