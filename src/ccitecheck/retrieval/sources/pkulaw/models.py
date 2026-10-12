"""北大法宝客户端的结构化返回值与错误类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class PkulawLawRecord:
    title: str
    url: Optional[str] = None
    category: list[str] = field(default_factory=list)
    document_no: Optional[str] = None
    issue_department: list[str] = field(default_factory=list)
    issue_date: Optional[str] = None
    implement_date: Optional[str] = None
    timeliness: list[str] = field(default_factory=list)
    effectiveness: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PkulawArticle(PkulawLawRecord):
    article_no: str = ""
    article_text: str = ""


@dataclass(frozen=True)
class PkulawRecognizedLaw:
    mentioned_title: str
    canonical_title: str
    fulltext: str
    url: Optional[str] = None

    @property
    def title(self) -> str:
        """兼容现有的法规同名匹配协议。"""
        return self.canonical_title


@dataclass(frozen=True)
class PkulawCaseRecord:
    title: str
    case_number: str = ""
    gid: str = ""
    court: str = ""
    last_instance_date: Optional[str] = None
    url: Optional[str] = None
    fulltext: Optional[str] = None
    holding: Optional[str] = None


class PkulawMcpError(RuntimeError):
    pass


class PkulawNotConfiguredError(PkulawMcpError):
    """本地没有配置可用的 MCP 凭证。"""


class PkulawNotFoundError(PkulawMcpError):
    """法宝检索已完成但未命中任何数据（区别于配置/网络错误）。"""


__all__ = [
    "PkulawArticle",
    "PkulawCaseRecord",
    "PkulawLawRecord",
    "PkulawMcpError",
    "PkulawNotConfiguredError",
    "PkulawNotFoundError",
    "PkulawRecognizedLaw",
]
