"""与具体数据源无关的法规溯源契约。

本模块定义溯源流水线的请求和结果边界，不包含 SQLite、北大法宝、HTTP
或回退顺序逻辑，使新增数据源适配器无需依赖其他厂商实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from ...domain.evidence import ArticleEvidence, LookupStatus, SourceTrace


@dataclass
class LookupRequest:
    """判定流水线发起的标准化法规查询。"""

    law_title: str
    article_no: Optional[str] = None
    context_text: str = ""
    query_text: Optional[str] = None
    existence_only: bool = False
    version_hint: Optional[str] = None
    jurisdiction: Optional[str] = None
    skip_nearby_scan: bool = False

@dataclass
class LookupResult:
    """单个数据源适配器返回的证据与查询轨迹。"""

    status: LookupStatus
    evidence: Optional[ArticleEvidence]
    trace: SourceTrace


@dataclass
class LocationCandidateResult:
    """二次定位查询返回的同一法规候选条文与独立轨迹。"""

    candidates: list[ArticleEvidence]
    trace: SourceTrace


class StatuteSource(Protocol):
    """所有法规数据源适配器共同实现的结构化接口。"""

    def lookup(self, request: LookupRequest) -> LookupResult:
        """查询一条标准化法规引用，不在此处作出判定。"""
        ...


__all__ = ["LocationCandidateResult", "LookupRequest", "LookupResult", "StatuteSource"]
