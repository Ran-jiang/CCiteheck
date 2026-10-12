"""单一权威来源执行服务。

本模块负责创建数据源和执行指定 source。它只返回证据和查询轨迹，不解释证据，也不产生
“通过、问题、无法判断”等判定结论。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..domain.evidence import (
    EvidenceCandidate,
    LookupStatus,
    RetrievalAttempt,
    RetrievalEvidence,
    RetrievalStatus,
    TechnicalStatus,
)
from ..domain.queries import SearchHypothesis
from ..query_construction.strategies.common import (
    build_case_keyword_query,
    build_case_semantic_query,
)
from .registry import SourceRegistry
from .sources.base import LookupRequest, LookupResult, StatuteSource
from .sources.local_laws import LocalSQLiteSource
from .sources.pkulaw.cases import CaseSearcher
from .sources.pkulaw.cache import CachedPkulawClient, cache_enabled
from .sources.pkulaw.client import PkulawMcpClient
from .sources.pkulaw.models import PkulawNotFoundError
from .sources.pkulaw.statutes import PkulawFallbackSource

try:
    DEFAULT_LOOKUP_WORKERS = max(
        1, min(4, int(os.getenv("PKULAW_LOOKUP_WORKERS", "4") or "4"))
    )
except ValueError:
    DEFAULT_LOOKUP_WORKERS = 4


def build_default_sources(
    db_path: str | Path,
    pkulaw_client: PkulawMcpClient | None = None,
) -> list[StatuteSource]:
    """创建默认法规溯源链：先查本地库，再回退到北大法宝。"""
    fallback_client = (
        CachedPkulawClient(pkulaw_client)
        if pkulaw_client is not None and cache_enabled()
        else pkulaw_client
    )
    return [LocalSQLiteSource(db_path), PkulawFallbackSource(fallback_client)]


def build_eu_sources() -> list[StatuteSource]:
    """创建欧盟法规溯源链：EUR-Lex 为主，Ansvar 作补充检索/复核。"""
    from .sources.ansvar import AnsvarSource
    from .sources.eurlex import EurLexSource

    return [EurLexSource(), AnsvarSource()]


def build_ansvar_sources() -> list[StatuteSource]:
    """具体涉外法域（DE/FR/JP/GB/US-CA…）溯源链：走 Ansvar。"""
    from .sources.ansvar import AnsvarSource

    return [AnsvarSource()]


def sources_for_jurisdiction(
    jurisdiction: str,
    *,
    cn_chain: list[StatuteSource] | None = None,
) -> list[StatuteSource]:
    """按法域返回对应来源链，保证首次检索与修复重试用同一链。

    - CN → 本地库 + 北大法宝（cn_chain，由 Scheduler 注入含本地库路径）
    - EU → EUR-Lex + Ansvar 补充
    - 具体涉外法域 → Ansvar
    - UNKNOWN → 空链（不自动检索，保留人工核查入口）
    """
    if jurisdiction == "EU":
        return build_eu_sources()
    if jurisdiction == "CN":
        return cn_chain or build_default_sources(_default_db_path())
    if jurisdiction in {"UNKNOWN", "FOREIGN"}:
        return []
    return build_ansvar_sources()


def _default_db_path() -> str:
    # ponytail: 延迟取默认库路径，避免模块加载期硬编码
    from ..infrastructure.paths import PROJECT_ROOT

    return str(PROJECT_ROOT / "data" / "laws.db")


def execute_source(source: StatuteSource, request: LookupRequest) -> LookupResult:
    """执行一次明确的数据源任务；不做 source fallback。"""
    return source.lookup(request)


@dataclass(frozen=True)
class RetrievalTask:
    kind: Literal["statute", "case"] = "statute"
    context_text: str = ""
    identity_title: str | None = None
    case_number: str | None = None
    case_name: str | None = None
    court: str | None = None
    source: StatuteSource | CaseSearcher | None = None


_STATUS_MAP = {
    LookupStatus.ARTICLE_FOUND: RetrievalStatus.FOUND,
    LookupStatus.RELEVANT_ARTICLES_FOUND: RetrievalStatus.FOUND,
    LookupStatus.LAW_FOUND_ARTICLE_MISSING: RetrievalStatus.ARTICLE_MISSING,
    LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE: RetrievalStatus.TEXT_UNAVAILABLE,
    LookupStatus.LAW_NOT_FOUND: RetrievalStatus.LAW_NOT_FOUND,
    LookupStatus.SOURCE_NOT_CONFIGURED: RetrievalStatus.SOURCE_NOT_CONFIGURED,
    LookupStatus.SOURCE_ERROR: RetrievalStatus.SOURCE_ERROR,
}


def build_statute_lookup_request(
    hypothesis: SearchHypothesis,
    *,
    context_text: str = "",
    identity_title: str | None = None,
    jurisdiction: str | None = None,
) -> LookupRequest:
    """由查询假设构造标准化法规请求；execute 与文档流水线共用同一映射规则。"""
    query_plan = hypothesis.query_plan
    identity = identity_title or (query_plan.target_name if query_plan else None) or next(
        (item.title for item in hypothesis.identity_candidates), ""
    )
    locator = hypothesis.normalized_locator
    article_no = (
        query_plan.article_no if query_plan else None
    ) or (locator.article_raw if locator else None)
    return LookupRequest(
        law_title=identity,
        article_no=article_no,
        context_text=context_text,
        query_text=query_plan.query_text if query_plan else None,
        version_hint=query_plan.version_hint if query_plan else None,
        existence_only=bool(
            query_plan
            and query_plan.route == "statute_related"
            and query_plan.query_text is None
        ),
        jurisdiction=jurisdiction,
    )


def execute(
    source_id: str,
    hypothesis: SearchHypothesis,
    task: RetrievalTask,
    *,
    registry: SourceRegistry | None = None,
) -> RetrievalEvidence:
    """按指定 hypothesis 和 source 执行一次可追溯检索。"""
    source = task.source or (registry.get(source_id) if registry else None)
    if task.kind == "case":
        submitted = {
            "case_number": task.case_number,
            "case_name": task.case_name,
            "court": task.court,
            "context_text": task.context_text,
        }
    else:
        request = build_statute_lookup_request(
            hypothesis,
            context_text=task.context_text,
            identity_title=task.identity_title,
        )
        submitted = {
            "law_title": request.law_title,
            "article_no": request.article_no,
            "context_text": request.context_text,
            "query_text": request.query_text,
            "version_hint": request.version_hint,
            "existence_only": request.existence_only,
        }
    if source is None:
        return RetrievalEvidence(
            claim_id=hypothesis.claim_id,
            hypothesis_id=hypothesis.hypothesis_id,
            source_id=source_id,
            submitted_request=submitted,
            retrieval_status=RetrievalStatus.SOURCE_NOT_CONFIGURED,
            technical_status=TechnicalStatus.SKIPPED,
        )
    if task.kind == "case":
        return _execute_case(source_id, hypothesis, task, source, submitted)
    try:
        result = execute_source(source, LookupRequest(**submitted))
    except Exception as exc:
        return RetrievalEvidence(
            claim_id=hypothesis.claim_id,
            hypothesis_id=hypothesis.hypothesis_id,
            source_id=source_id,
            submitted_request=submitted,
            retrieval_status=RetrievalStatus.SOURCE_ERROR,
            technical_status=TechnicalStatus.SOURCE_ERROR,
            provider_metadata={"error": str(exc)},
        )
    candidates: list[EvidenceCandidate] = []
    if result.evidence is not None:
        evidence = result.evidence
        candidates.append(EvidenceCandidate(
            kind="statute",
            title=evidence.law_title,
            locator=evidence.article_no,
            text=evidence.article_text,
            source_url=result.trace.source_url,
            metadata=evidence.source_metadata,
        ))
        candidates.extend(EvidenceCandidate(
            kind="statute",
            title=evidence.law_title,
            locator=item.locator or item.article_no,
            text=item.article_text,
            source_url=result.trace.source_url,
            metadata={"relevance_score": item.relevance_score},
        ) for item in evidence.related_articles)
    route_attempts = [RetrievalAttempt(
        mode=str(item.get("route") or item.get("mode") or "provider"),
        status=str(item.get("status") or "completed"),
        request=dict(item.get("request") or {}),
        message=str(item.get("message") or ""),
    ) for item in result.trace.metadata.get("route_attempts", []) if isinstance(item, dict)]
    retrieval_status = _STATUS_MAP.get(result.status, RetrievalStatus.TEXT_UNAVAILABLE)
    return RetrievalEvidence(
        claim_id=hypothesis.claim_id,
        hypothesis_id=hypothesis.hypothesis_id,
        source_id=source_id,
        submitted_request=submitted,
        route_attempts=route_attempts,
        retrieval_status=retrieval_status,
        technical_status=(
            TechnicalStatus.SOURCE_ERROR
            if retrieval_status == RetrievalStatus.SOURCE_ERROR
            else TechnicalStatus.COMPLETED
        ),
        candidates=candidates,
        provider_metadata=result.trace.metadata,
    )


def _execute_case(
    source_id: str,
    hypothesis: SearchHypothesis,
    task: RetrievalTask,
    source: CaseSearcher,
    submitted: dict,
) -> RetrievalEvidence:
    """执行一个案例来源的 provider 内部精准/语义路由。"""
    title, fulltext = build_case_keyword_query(
        task.case_name,
        task.context_text,
        task.court,
    )
    if task.case_number:
        title, fulltext = "", task.case_number
    attempts: list[RetrievalAttempt] = []
    try:
        try:
            records = source.search_keyword(title, fulltext)
        except PkulawNotFoundError:
            records = []
        attempts.append(RetrievalAttempt(
            mode="case_exact",
            status="completed" if records else "not_found",
            request={"title": title, "fulltext": fulltext},
        ))
        if not records:
            semantic_query = build_case_semantic_query(
                task.case_name,
                task.context_text,
                task.court,
            )
            try:
                records = source.search_semantic(semantic_query)
            except PkulawNotFoundError:
                records = []
            attempts.append(RetrievalAttempt(
                mode="case_semantic",
                status="completed" if records else "not_found",
                request={"text": semantic_query},
            ))
    except Exception as exc:
        return RetrievalEvidence(
            claim_id=hypothesis.claim_id,
            hypothesis_id=hypothesis.hypothesis_id,
            source_id=source_id,
            submitted_request=submitted,
            route_attempts=attempts,
            retrieval_status=RetrievalStatus.SOURCE_ERROR,
            technical_status=TechnicalStatus.SOURCE_ERROR,
            provider_metadata={"error": str(exc)},
        )

    return RetrievalEvidence(
        claim_id=hypothesis.claim_id,
        hypothesis_id=hypothesis.hypothesis_id,
        source_id=source_id,
        submitted_request=submitted,
        route_attempts=attempts,
        retrieval_status=(
            RetrievalStatus.FOUND if records else RetrievalStatus.LAW_NOT_FOUND
        ),
        technical_status=TechnicalStatus.COMPLETED,
        candidates=[EvidenceCandidate(
            kind="case",
            title=record.title,
            locator=record.case_number,
            text=record.holding or record.fulltext,
            source_url=record.url,
            metadata={
                "gid": record.gid,
                "court": record.court,
                "last_instance_date": record.last_instance_date,
            },
        ) for record in records],
    )


__all__ = [
    "RetrievalTask",
    "build_ansvar_sources",
    "build_default_sources",
    "build_eu_sources",
    "build_statute_lookup_request",
    "execute",
    "execute_source",
    "sources_for_jurisdiction",
]
