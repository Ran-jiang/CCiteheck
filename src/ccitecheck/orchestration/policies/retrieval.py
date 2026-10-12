"""Scheduler 的数据源回退策略。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
from typing import Callable, Mapping, TypeVar

from ...domain.evidence import ArticleEvidence, ArticleExcerpt, LookupStatus, SourceTrace, SourceTier
from ...domain.queries import RerankBatch, RerankCandidate
from ...domain.runs import DEFAULT_TECHNICAL_RETRY_LIMIT
from ...infrastructure.database import normalize_article_key
from ...query_construction.matching import equivalent_law_titles
from ...retrieval.service import DEFAULT_LOOKUP_WORKERS, execute_source
from ...retrieval.sources.base import LookupRequest, LookupResult, StatuteSource
from ...retrieval.sources.pkulaw.urls import is_legacy_mcp_url
from ...infrastructure.debug_timing import bind_current_timer, measure


_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")


def run_with_technical_retries(
    operation: Callable[[], _T],
    is_retryable: Callable[[_T], bool],
    *,
    max_retries: int = DEFAULT_TECHNICAL_RETRY_LIMIT,
) -> tuple[_T, list[_T]]:
    """Run one source operation with the shared technical-retry budget."""
    attempts: list[_T] = []
    while True:
        result = operation()
        attempts.append(result)
        if not is_retryable(result) or len(attempts) > max_retries:
            return result, attempts


def lookup_with_chain(
    sources: list[StatuteSource], request: LookupRequest, reranker=None
) -> tuple[LookupResult, list[SourceTrace]]:
    if not request.article_no and request.query_text:
        return _lookup_related_with_chain(sources, request, reranker)
    attempts: list[SourceTrace] = []
    last_result: LookupResult | None = None
    best_partial: LookupResult | None = None
    local_complete_with_legacy_url: LookupResult | None = None
    local_without_link: LookupResult | None = None
    for source in sources:
        result, source_attempts = run_with_technical_retries(
            lambda: execute_source(source, request),
            lambda item: item.status == LookupStatus.SOURCE_ERROR,
        )
        attempts.extend(item.trace for item in source_attempts)
        last_result = result
        if local_without_link is not None:
            _fill_verified_link(local_without_link, result)
            if local_without_link.trace.source_url:
                return local_without_link, attempts
            continue
        if result.status in {LookupStatus.ARTICLE_FOUND, LookupStatus.RELEVANT_ARTICLES_FOUND}:
            if is_legacy_mcp_url(result.trace.source_url):
                discarded = result.trace.source_url
                result.trace.source_url = None
                result.trace.metadata["discarded_legacy_source_url"] = discarded
                if result.evidence is not None:
                    result.evidence.data_source.source_url = None
                if result.trace.tier == SourceTier.LOCAL_SQLITE:
                    local_without_link = result
                else:
                    local_complete_with_legacy_url = result
                continue
            if result.trace.tier == SourceTier.LOCAL_SQLITE and not result.trace.source_url:
                local_without_link = result
                continue
            return result, attempts
        if result.status in {
            LookupStatus.LAW_FOUND_ARTICLE_MISSING,
            LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE,
        }:
            best_partial = result
    if last_result is None:
        raise ValueError("No statute sources configured")
    return local_without_link or local_complete_with_legacy_url or best_partial or last_result, attempts


def _fill_verified_link(original: LookupResult, remote: LookupResult) -> None:
    if not original.evidence or not remote.evidence or not remote.trace.source_url or is_legacy_mcp_url(remote.trace.source_url):
        return
    version = original.evidence.source_metadata.get("version_key") or original.trace.metadata.get("version_key")
    other_version = remote.evidence.source_metadata.get("version_key") or remote.trace.metadata.get("version_key")
    if not version or version != other_version or not equivalent_law_titles(original.evidence.law_title, remote.evidence.law_title):
        return
    original.trace.source_url = remote.trace.source_url
    original.evidence.data_source.source_url = remote.trace.source_url
    original.trace.metadata["link_evidence"] = remote.trace.model_dump(mode="json")


def run_lookup_batch(
    sources: list[StatuteSource],
    requests: Mapping[tuple, LookupRequest],
    workers: int = DEFAULT_LOOKUP_WORKERS,
    reranker=None,
) -> dict[tuple, tuple[LookupResult, list[SourceTrace]]]:
    if not requests:
        return {}
    keys = list(requests)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(
            bind_current_timer(
                lambda key: lookup_with_chain(sources, requests[key], reranker)
            ),
            keys,
        ))
    return dict(zip(keys, outcomes))


def _lookup_related_with_chain(
    sources: list[StatuteSource], request: LookupRequest, reranker
) -> tuple[LookupResult, list[SourceTrace]]:
    """多来源召回合并后统一重排；低相关候选允许全部淘汰。"""
    attempts: list[SourceTrace] = []
    results: list[LookupResult] = []
    candidates: dict[tuple, tuple[RerankCandidate, set[str]]] = {}
    provenance = {}
    for source in sources:
        result, source_attempts = run_with_technical_retries(
            lambda: execute_source(source, request),
            lambda item: item.status == LookupStatus.SOURCE_ERROR,
        )
        results.append(result)
        attempts.extend(item.trace for item in source_attempts)
        evidence = result.evidence
        if evidence is None or not equivalent_law_titles(evidence.law_title, request.law_title):
            continue
        rows = list(evidence.related_articles)
        if evidence.article_no and evidence.article_text:
            rows.append(ArticleExcerpt(
                article_no=evidence.article_no,
                article_text=evidence.article_text,
                relevance_score=1.0,
            ))
        for row in rows:
            locator = row.locator or row.article_no
            normalized_locator = (
                normalize_article_key(locator)
                if row.locator_type == "article"
                else "".join(locator.split())
            )
            # 一次 related 请求已经锁定同一目标法规；不同来源常分别返回
            # “法名”和“法名（年份修订）”，应按条文定位去重。
            version_key = row.version_key or evidence.source_metadata.get("version_key")
            key = (row.locator_type, normalized_locator, version_key)
            existing = candidates.get(key)
            if existing is None:
                candidate = RerankCandidate(
                    candidate_id=f"related_{len(candidates) + 1:03d}",
                    law_title=evidence.law_title,
                    article_no=row.article_no,
                    locator=row.locator,
                    locator_type=row.locator_type,
                    article_text=row.article_text,
                )
                candidates[key] = (candidate, {result.trace.source_name})
                provenance[candidate.candidate_id] = row.model_copy(update={
                    "law_title": row.law_title or evidence.law_title,
                    "version_key": version_key,
                    "source_url": row.source_url or result.trace.source_url,
                })
            else:
                existing[1].add(result.trace.source_name)

    base = next((item for item in results if item.evidence is not None), None)
    if base is None:
        if not results:
            raise ValueError("No statute sources configured")
        return results[-1], attempts
    if not candidates:
        return _related_empty_result(base, "未召回可供相关性判断的条文"), attempts

    rerank = getattr(reranker, "rerank_related", None)
    if not callable(rerank):
        # 模型未配置不抹掉已召回的原文；保持待核查，不据 BM25 分数判通过。
        base.trace.metadata["related_pending"] = True
        base.trace.message = "已召回相关条文待核查"
        return base, attempts
    ordered = [item[0] for item in candidates.values()]
    try:
        with measure("retrieval.reranker"):
            batch = RerankBatch.model_validate(
                rerank(request.query_text or "", ordered)
            )
        decisions = batch.results
        expected = {item.candidate_id for item in ordered}
        returned = [item.candidate_id for item in decisions]
        if len(returned) != len(set(returned)) or set(returned) != expected:
            raise ValueError("重排结果缺少、重复或包含未知 candidate_id")
    except Exception as exc:
        _LOGGER.exception("相关条款重排失败：%s", exc)
        return _related_empty_result(
            base, "模型服务暂时不可用", failure="model_unavailable"
        ), attempts

    by_id = {item.candidate_id: item for item in ordered}
    selected = [
        (decision.score, by_id[decision.candidate_id])
        for decision in decisions
        if decision.relevant
        and decision.score >= 0.65
        and decision.candidate_id in by_id
    ]
    selected.sort(key=lambda item: item[0], reverse=True)
    if not selected:
        result = _related_empty_result(base, "候选条文均未通过相关性门槛")
        result.trace.metadata.update(
            candidate_count=len(ordered), all_candidates_rejected=True,
        )
        return result, attempts

    excerpts = [provenance[candidate.candidate_id].model_copy(update={"relevance_score": score})
                for score, candidate in selected]
    trace = base.trace.model_copy(deep=True)
    trace.status = LookupStatus.RELEVANT_ARTICLES_FOUND
    trace.source_name = "CCitecheck 多来源法条召回与 LLM 重排"
    trace.message = f"已召回并保留 {len(excerpts)} 条强相关法条"
    trace.metadata.update(
        retrieval_method="bm25_pkulaw_semantic_llm_rerank",
        query_text=request.query_text,
        candidate_count=len(ordered),
        accepted_count=len(excerpts),
        relevance_threshold=0.65,
        candidate_channels={
            candidate.candidate_id: sorted(channels)
            for candidate, channels in candidates.values()
        },
    )
    evidence = ArticleEvidence(
        law_title=excerpts and selected[0][1].law_title or request.law_title,
        source_type=base.evidence.source_type if base.evidence else "law",
        article_text="\n\n".join(
            f"{item.locator or item.article_no}　{item.article_text}" for item in excerpts
        ),
        version_label=base.evidence.version_label if base.evidence else None,
        version_status=base.evidence.version_status if base.evidence else None,
        source_metadata={
            **(base.evidence.source_metadata if base.evidence else {}),
            "retrieval_method": "bm25_pkulaw_semantic_llm_rerank",
            "query_text": request.query_text,
        },
        related_articles=excerpts,
        data_source=trace,
    )
    return LookupResult(trace.status, evidence, trace), attempts


def _related_empty_result(
    base: LookupResult, message: str, *, failure: str | None = None
) -> LookupResult:
    trace = base.trace.model_copy(deep=True)
    trace.status = LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
    trace.message = message
    if failure:
        trace.metadata["related_failure"] = failure
    evidence = base.evidence.model_copy(deep=True) if base.evidence else None
    if evidence is not None:
        evidence.article_no = None
        evidence.article_text = None
        evidence.related_articles = []
        evidence.data_source = trace
    return LookupResult(trace.status, evidence, trace)


__all__ = ["lookup_with_chain", "run_lookup_batch", "run_with_technical_retries"]
