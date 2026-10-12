"""北大法宝法规回退源及其确定性三级路由。"""

from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import re
from typing import Optional, Protocol

from ....domain.evidence import (
    ArticleEvidence,
    ArticleExcerpt,
    LookupStatus,
    SourceTier,
    SourceTrace,
)
from ....domain.law_titles import cn_title_shape_key, cn_title_shape_variants
from ....query_construction.strategies.common import (
    _compact_context,
    build_article_exact_title,
    build_law_semantic_query,
    has_substantive_content,
)
from ...ranking import retrieve_relevant_articles
from ..base import LocationCandidateResult, LookupRequest, LookupResult
from .cache import CachedPkulawClient, cache_enabled
from .client import (
    PkulawArticle,
    PkulawLawRecord,
    PkulawMcpClient,
    PkulawMcpError,
    PkulawNotConfiguredError,
    PkulawNotFoundError,
    normalize_article_no,
)
from .corpus import ParsedCorpus, split_recognized_fulltext
from ....query_construction.matching import (
    equivalent_law_titles,
    match_law_record,
    matches_requested_law_title,
)
from ....infrastructure.database import normalize_title, strip_version_annotation
from ....infrastructure.debug_timing import bind_current_timer, measure


class PkulawFallbackSource:
    def __init__(self, client: Optional["StatuteLookupClient"] = None):
        self.client = client

    def _recall_similar_titles(self, law_title: str) -> list[str]:
        """精确法名未命中时，用两段递减前缀和一段后缀召回近似法名。"""
        core = cn_title_shape_key(strip_version_annotation(normalize_title(law_title)))
        if len(core) < 6:
            return []
        titles: list[str] = []
        seen: set[str] = set()
        queries = [
            core[:length]
            for length in (len(core) * 2 // 3, len(core) // 2)
            if length >= 4
        ]
        queries.append(core[-4:])
        for query in dict.fromkeys(queries):
            try:
                records = self._client().get_law_list(title=query)
            except (PkulawNotFoundError, PkulawMcpError):
                continue
            for record in records:
                if record.title and record.title not in seen:
                    seen.add(record.title)
                    titles.append(record.title)
            if titles:
                break
        return titles

    def lookup(self, request: LookupRequest) -> LookupResult:
        return (
            self._lookup_article(request)
            if request.article_no
            else self._lookup_without_article(request)
        )

    def locate_candidates(self, request: LookupRequest) -> LocationCandidateResult:
        """通过北大法宝语义检索召回同一法规的定位候选。"""
        trace = self._trace()
        trace.metadata["route_attempts"].append({
            "service": "law_semantic",
            "purpose": "citation_location",
            "status": "started",
        })
        attempt = trace.metadata["route_attempts"][-1]
        try:
            articles = self._client().search_law_articles(
                build_law_semantic_query(request.context_text, request.law_title)
            )
        except PkulawNotFoundError:
            articles = []
            attempt["status"] = "not_found"
        except PkulawMcpError as exc:
            attempt.update(status="error", message=str(exc))
            trace.status = _pkulaw_error_status(exc)
            trace.message = str(exc)
            return LocationCandidateResult([], trace)

        filtered = [
            article
            for article in articles
            if match_law_record(request.law_title, [article]) is not None
        ]
        attempt.update(status="completed", candidate_count=len(filtered))
        cited = normalize_article_no(request.article_no or "")
        if request.article_no and not request.skip_nearby_scan and (
            not filtered
            or all(
                not article.article_no
                or normalize_article_no(article.article_no) == cited
                for article in filtered
            )
        ):
            filtered.extend(self._scan_nearby_articles(request, trace))
        filtered = _top_articles(request.context_text, filtered, limit=3)
        law_item = getattr(self._client(), "get_law_item_content", None)
        if callable(law_item):
            enriched = []
            for article in filtered:
                if article.timeliness:
                    enriched.append(article)
                    continue
                item_attempt = {
                    "service": "law_item",
                    "purpose": "candidate_version_enrichment",
                    "article_no": article.article_no,
                    "status": "started",
                }
                trace.metadata["route_attempts"].append(item_attempt)
                try:
                    item = law_item(article.title, article.article_no)
                except PkulawNotFoundError:
                    item_attempt["status"] = "not_found"
                except PkulawMcpError as exc:
                    item_attempt.update(status="error", message=str(exc))
                else:
                    if matches_requested_law_title(article.title, item.title):
                        article = item
                        item_attempt["status"] = "completed"
                    else:
                        item_attempt.update(status="mismatched", returned_title=item.title)
                enriched.append(article)
            filtered = enriched
        if not filtered and request.article_no:
            # 同法内无果：直通该法全部版本编目，逐版本精确查所引条号（版本错）
            filtered = self._scan_version_articles(request, trace)
        if not filtered:
            # 版本直通无果：放开法名过滤做跨法语义检索（法名错）
            filtered = self._scan_cross_law_articles(request, trace)
        # 语义/版本/跨法三条路线最终都补齐时效；否则调度层无法阻止废止法
        # 仅凭文本相似度成为纠错目标。
        filtered = [
            article
            if article.timeliness
            else self._enrich_timeliness(article, trace.metadata["route_attempts"])
            for article in filtered
        ]
        trace.status = (
            LookupStatus.RELEVANT_ARTICLES_FOUND
            if filtered
            else LookupStatus.LAW_NOT_FOUND
        )
        trace.message = "定位候选检索完成"
        return LocationCandidateResult(
            candidates=[self._candidate_evidence(request, trace, article) for article in filtered],
            trace=trace,
        )

    def _scan_nearby_articles(self, request: LookupRequest, trace: SourceTrace) -> list[PkulawArticle]:
        """语义服务无结果时，对引用条号附近做受限精确扫描。"""
        from ....domain.legal_numbers import chinese_number_to_int

        match = re.fullmatch(r"第([一二三四五六七八九十百千万两零〇0-9]+)条", request.article_no or "")
        if not match:
            return []
        token = match.group(1)
        try:
            cited = int(token) if token.isdigit() else chinese_number_to_int(token)
        except ValueError:
            return []
        titles = cn_title_shape_variants(normalize_title(request.law_title))

        def fetch(number: int):
            for title in titles:
                law_item = getattr(self._client(), "get_law_item_content", None)
                if callable(law_item):
                    try:
                        article = law_item(title, f"第{number}条")
                        if matches_requested_law_title(title, article.title):
                            return article
                    except (PkulawNotFoundError, PkulawMcpError):
                        pass
                try:
                    article = self._client().get_article(title, f"第{number}条")
                    if matches_requested_law_title(title, article.title):
                        return article
                except (PkulawNotFoundError, PkulawMcpError):
                    continue
            return None

        def fetch_many(numbers: list[int]) -> list[PkulawArticle]:
            with ThreadPoolExecutor(max_workers=min(6, len(numbers))) as pool:
                return [
                    article
                    for article in pool.map(bind_current_timer(fetch), numbers)
                    if article is not None
                ]

        narrow = list(range(max(1, cited - 2), cited + 3))
        with measure("retrieval.pkulaw_nearby_scan"):
            articles = fetch_many(narrow)
            matched = [
                article for article in articles
                if match_law_record(request.law_title, [article])
            ]
            selected = _top_articles(request.context_text, matched, limit=3)
            expanded = not selected
            requested = list(narrow)
            if expanded:
                narrow_set = set(narrow)
                remaining = [
                    number
                    for number in range(max(1, cited - 10), cited + 11)
                    if number not in narrow_set
                ]
                requested.extend(remaining)
                articles.extend(fetch_many(remaining))
                matched = [
                    article for article in articles
                    if match_law_record(request.law_title, [article])
                ]
                selected = _top_articles(request.context_text, matched, limit=3)
        trace.metadata["route_attempts"].append({
            "service": "law_exact_nearby_scan",
            "purpose": "citation_location",
            "status": "completed",
            "range": [min(requested), max(requested)],
            "expanded": expanded,
            "requested_count": len(requested),
            "matched_count": len(matched),
            "candidate_count": len(selected),
        })
        return selected

    def _scan_version_articles(self, request: LookupRequest, trace: SourceTrace) -> list[PkulawArticle]:
        """版本直通：列出同法全部版本编目，逐版本精确查所引条号。

        只召回不作结论；是否构成版本错误由调度层的确认规则判定。
        """
        if not request.article_no:
            return []
        try:
            records = self._client().get_law_list(title=request.law_title)
        except PkulawNotFoundError:
            return []
        except PkulawMcpError as exc:
            trace.metadata["route_attempts"].append({
                "service": "law_list", "purpose": "version_scan",
                "status": "error", "message": str(exc),
            })
            return []
        versions: list[PkulawLawRecord] = []
        seen: set[str] = set()
        for record in records:
            if match_law_record(request.law_title, [record]) is None:
                continue
            key = normalize_title(record.title)
            if key in seen:
                continue
            seen.add(key)
            versions.append(record)
        attempt = {
            "service": "law_list", "purpose": "version_scan", "status": "completed",
            "candidate_titles": [record.title for record in versions],
        }
        trace.metadata["route_attempts"].append(attempt)
        hits: list[PkulawArticle] = []
        for record in versions[:6]:
            try:
                article = self._client().get_article(record.title, request.article_no)
            except (PkulawNotFoundError, PkulawMcpError):
                continue
            if article and matches_requested_law_title(record.title, article.title):
                hits.append(article)
        attempt["hit_titles"] = [article.title for article in hits]
        return hits

    def _scan_cross_law_articles(self, request: LookupRequest, trace: SourceTrace) -> list[PkulawArticle]:
        """跨法直通：不带法名过滤做语义条文检索，召回别法候选。

        只召回不作结论；是否构成法名错误由调度层的确认规则判定。
        """
        proposition = _compact_context(request.context_text or "", excluded=(request.law_title,))
        if not proposition:
            return []
        query = f"检索与以下引用表述最相关的具体条文：{proposition}"[:500]
        attempt = {
            "service": "law_semantic", "purpose": "cross_law_location", "status": "started",
        }
        trace.metadata["route_attempts"].append(attempt)
        try:
            articles = self._client().search_law_articles(query)
        except PkulawNotFoundError:
            articles = []
            attempt["status"] = "not_found"
        except PkulawMcpError as exc:
            attempt.update(status="error", message=str(exc))
            return []
        attempt.update(status="completed", candidate_count=len(articles))
        return _top_articles(request.context_text or "", articles, limit=3)

    def _trace(self) -> SourceTrace:
        return SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="北大法宝 MCP",
            status=LookupStatus.LAW_NOT_FOUND,
            metadata={"route_attempts": []},
        )

    def _lookup_article(self, request: LookupRequest) -> LookupResult:
        trace = self._trace()
        attempts = trace.metadata["route_attempts"]
        query_titles = _versioned_query_titles(request.law_title, request.version_hint)
        title = query_titles[0]
        client = self._client()
        exact_absent = True
        law_item_absent: bool | None = None
        law_item = getattr(client, "get_law_item_content", None)
        if callable(law_item):
            law_item_absent = True
            for query_title in query_titles:
                attempt = {
                    "service": "law_item",
                    "title_shape": query_title,
                    "status": "started",
                }
                attempts.append(attempt)
                try:
                    article = law_item(query_title, request.article_no or "")
                except PkulawNotFoundError:
                    attempt["status"] = "not_found"
                    continue
                except PkulawMcpError as exc:
                    law_item_absent = False
                    attempt.update(status="error", message=str(exc))
                    continue
                exact_absent = False
                law_item_absent = False
                if matches_requested_law_title(query_title, article.title):
                    attempt["status"] = "completed"
                    article = self._enrich_timeliness(article, attempts)
                    return self._article_result(request, trace, article)
                attempt.update(status="mismatched", returned_title=article.title)

        for query_title in query_titles:
            attempt = {
                "service": "law_search_get_article",
                "title_shape": query_title,
                "status": "started",
            }
            attempts.append(attempt)
            try:
                article = client.get_article(query_title, request.article_no or "")
            except PkulawNotFoundError:
                attempt["status"] = "not_found"
                continue
            except PkulawMcpError as exc:
                attempt.update(status="error", message=str(exc))
                continue
            exact_absent = False
            if matches_requested_law_title(query_title, article.title):
                attempt["status"] = "completed"
                article = self._enrich_timeliness(article, attempts)
                return self._article_result(request, trace, article)
            attempt.update(status="mismatched", returned_title=article.title)

        recognize = getattr(client, "recognize_laws", None)
        if exact_absent and callable(recognize):
            attempts.append({"service": "law_recognition", "status": "started"})
            try:
                recognized_laws = recognize(title)
                recognized = match_law_record(request.law_title, recognized_laws)
                attempts[-1].update(
                    status="completed" if recognized else "not_found",
                    candidate_count=len(recognized_laws),
                )
                if (
                    recognized is not None
                    and normalize_title(recognized.canonical_title)
                    not in {normalize_title(item) for item in query_titles}
                ):
                    title = recognized.canonical_title
                    exact_routes = []
                    if callable(law_item):
                        exact_routes.append(("law_item", law_item))
                    exact_routes.append(("law_search_get_article", client.get_article))
                    for service, fetch in exact_routes:
                        attempts.append({
                            "service": service,
                            "purpose": "canonical_title_retry",
                            "status": "started",
                        })
                        try:
                            article = fetch(title, request.article_no or "")
                        except PkulawNotFoundError:
                            attempts[-1]["status"] = "not_found"
                            continue
                        except PkulawMcpError as exc:
                            attempts[-1].update(status="error", message=str(exc))
                            continue
                        if matches_requested_law_title(title, article.title):
                            attempts[-1]["status"] = "completed"
                            article = self._enrich_timeliness(article, attempts)
                            return self._article_result(request, trace, article)
                        attempts[-1].update(
                            status="mismatched", returned_title=article.title
                        )
            except PkulawNotFoundError:
                attempts[-1]["status"] = "not_found"
            except PkulawMcpError as exc:
                attempts[-1].update(status="error", message=str(exc))

        attempts.append({"service": "law_semantic_exact", "status": "started"})
        try:
            articles = self._client().search_law_articles_for_article(
                title, request.article_no or ""
            )
            attempts[-1].update(status="completed", candidate_count=len(articles))
        except PkulawNotFoundError:
            articles = []
            attempts[-1]["status"] = "not_found"
        except PkulawMcpError as exc:
            articles = []
            attempts[-1].update(status="error", message=str(exc))

        filtered = [a for a in articles if matches_requested_law_title(title, a.title)]
        wanted = normalize_article_no(request.article_no or "")
        exact = next(
            (a for a in filtered if normalize_article_no(a.article_no) == wanted), None
        )
        if exact is not None:
            exact = self._enrich_timeliness(exact, attempts)
            return self._article_result(request, trace, exact)
        related = _rank_articles(request.context_text, filtered)
        if related and not exact_absent:
            return self._related_result(
                request,
                trace,
                filtered[0],
                related,
                "精确查询未命中，以下为该法规内语义召回的相关条款",
            )

        records = []
        for query_title in query_titles:
            attempt = {
                "service": "law_keyword",
                "title_shape": query_title,
                "status": "started",
            }
            attempts.append(attempt)
            try:
                records = self._client().get_law_list(title=query_title)
                attempt.update(
                    status="completed" if records else "not_found",
                    candidate_count=len(records),
                )
                if records:
                    title = query_title
                    if _match_requested_record(query_title, records) is not None:
                        break
            except PkulawNotFoundError:
                attempt["status"] = "not_found"
            except PkulawMcpError as exc:
                attempt.update(status="error", message=str(exc))
                return self._error(trace, exc)
        matched = _match_requested_record(title, records)
        if matched is None:
            trace.status = LookupStatus.LAW_NOT_FOUND
            trace.message = "北大法宝检索完成，未找到该法规"
            recalled = self._recall_similar_titles(request.law_title)
            trace.metadata.update(
                search_completed=True,
                candidate_titles=[*(r.title for r in records[:5]), *recalled],
            )
            return LookupResult(trace.status, None, trace)
        trace.status = LookupStatus.LAW_FOUND_ARTICLE_MISSING
        trace.message = f"北大法宝已收录该法规，但未返回{request.article_no}"
        trace.source_url = matched.url
        trace.metadata.update(search_completed=True, **_law_record_metadata(matched))
        if exact_absent:
            confirmation = (
                "double_signal" if law_item_absent is True else "single_signal"
            )
            trace.metadata["article_absence_confirmation"] = confirmation
            trace.metadata["article_absent_confirmed"] = True
        evidence = self._metadata_evidence(request, trace, matched)
        if related:
            evidence = evidence.model_copy(update={"related_articles": related})
        return LookupResult(trace.status, evidence, trace)

    def _lookup_without_article(self, request: LookupRequest) -> LookupResult:
        trace = self._trace()
        attempts = trace.metadata["route_attempts"]
        client = self._client()
        query_titles = _versioned_query_titles(request.law_title, request.version_hint)
        title = query_titles[0]
        recognized = None
        recognize = getattr(client, "recognize_laws", None)
        if callable(recognize):
            for query_title in query_titles:
                attempt = {
                    "service": "law_recognition",
                    "title_shape": query_title,
                    "status": "started",
                }
                attempts.append(attempt)
                try:
                    recognized_laws = recognize(query_title)
                    recognized = match_law_record(request.law_title, recognized_laws)
                    attempt.update(
                        status="completed" if recognized else "not_found",
                        candidate_count=len(recognized_laws),
                    )
                    if recognized:
                        break
                except PkulawNotFoundError:
                    attempt["status"] = "not_found"
                except PkulawMcpError as exc:
                    attempt.update(status="error", message=str(exc))

        canonical_title = recognized.canonical_title if recognized else title
        law_titles = cn_title_shape_variants(canonical_title)
        records = []
        for query_title in law_titles:
            attempt = {
                "service": "law_keyword",
                "title_shape": query_title,
                "status": "started",
            }
            attempts.append(attempt)
            try:
                records = client.get_law_list(title=query_title)
                attempt.update(
                    status="completed" if records else "not_found",
                    candidate_count=len(records),
                )
                if records:
                    canonical_title = query_title
                    if _match_requested_record(query_title, records) is not None:
                        break
            except PkulawNotFoundError:
                attempt["status"] = "not_found"
            except PkulawMcpError as exc:
                attempt.update(status="error", message=str(exc))
                return self._error(trace, exc)
        matched = _match_requested_record(canonical_title, records)
        query_text = request.query_text or request.context_text
        if matched is not None and (
            request.existence_only
            or (
                request.query_text is None
                and not has_substantive_content(query_text, request.law_title)
            )
        ):
            return self._law_text_unavailable(request, trace, matched, records)

        if matched is not None and recognized is not None:
            corpus = split_recognized_fulltext(recognized.fulltext)
            attempts.append({
                "service": "law_recognition_fulltext",
                "status": "completed" if corpus.structurally_valid else "invalid",
                "structure": corpus.structure,
                "chunk_count": len(corpus.chunks),
                "warnings": list(corpus.warnings),
            })
            if corpus.structurally_valid:
                related = retrieve_relevant_articles(
                    query_text,
                    [
                        {
                            "article_key": chunk.sequence,
                            "article_no": chunk.article_no,
                            "locator": chunk.locator,
                            "locator_type": chunk.locator_type,
                            "text": chunk.text,
                        }
                        for chunk in corpus.chunks
                    ],
                    limit=8,
                )
                if related:
                    return self._recognized_corpus_result(
                        trace, matched, records, recognized, corpus, related
                    )

        attempts.append({"service": "law_semantic", "status": "started"})
        semantic_error: PkulawMcpError | None = None
        try:
            articles = client.search_law_articles(
                build_law_semantic_query(query_text, canonical_title)
            )
            attempts[-1].update(status="completed", candidate_count=len(articles))
        except PkulawNotFoundError:
            articles = []
            attempts[-1]["status"] = "not_found"
        except PkulawMcpError as exc:
            articles = []
            semantic_error = exc
            attempts[-1].update(status="error", message=str(exc))
        filtered = [
            a for a in articles if match_law_record(canonical_title, [a]) is not None
        ]
        related = _rank_articles(query_text, filtered, limit=8)
        if related:
            return self._related_result(
                request, trace, filtered[0], related, "已通过法规语义检索召回相关条款"
            )
        if matched is not None:
            return self._law_text_unavailable(request, trace, matched, records)
        if semantic_error is not None:
            return self._error(trace, semantic_error)
        trace.status = LookupStatus.LAW_NOT_FOUND
        trace.message = "北大法宝检索完成，未找到该法规"
        trace.metadata.update(
            search_completed=True,
            candidate_titles=[
                *(r.title for r in records[:5]),
                *self._recall_similar_titles(request.law_title),
            ],
        )
        return LookupResult(trace.status, None, trace)

    def _recognized_corpus_result(
        self, trace, matched, records, recognized, corpus: ParsedCorpus, related
    ):
        trace.status = LookupStatus.RELEVANT_ARTICLES_FOUND
        trace.message = (
            "已在北大法宝返回的目标法规条文内召回相关法条"
            if corpus.structure == "article"
            else "已在北大法宝返回的目标规范性文件内召回相关段落"
        )
        trace.source_url = recognized.url or matched.url
        trace.metadata.update(
            **_law_record_metadata(matched),
            retrieval_method=(
                "pkulaw_recognition_article_bm25"
                if corpus.structure == "article"
                else "pkulaw_recognition_paragraph_bm25"
            ),
            identity_confirmed_by="law_recognition+law_keyword",
            recognized_surface_title=recognized.mentioned_title,
            recognized_canonical_title=recognized.canonical_title,
            corpus_structure=corpus.structure,
            corpus_chunk_count=len(corpus.chunks),
            corpus_completeness="provider_returned_text_unverified",
            corpus_warnings=list(corpus.warnings),
            candidates=[_law_record_metadata(record) for record in records],
        )
        evidence = ArticleEvidence(
            law_title=recognized.canonical_title,
            source_type=_evidence_source_type(matched),
            article_text="\n\n".join(_format_excerpt(item) for item in related),
            version_label=_first(matched.timeliness),
            version_status=_first(matched.timeliness),
            effective_from=matched.implement_date,
            source_metadata=trace.metadata,
            related_articles=related,
            data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace)

    def _enrich_timeliness(self, article, attempts):
        title = article.title
        attempt = {
            "service": "law_keyword",
            "purpose": "timeliness_enrichment",
            "status": "started",
        }
        attempts.append(attempt)
        try:
            records = self._client().get_law_list(title=title)
            matched = _match_requested_record(title, records)
            attempt.update(
                status="completed",
                candidate_count=len(records),
                candidates=[_law_record_metadata(record) for record in records],
            )
            current = next((
                record for record in records
                if equivalent_law_titles(record.title, title)
                and any("现行有效" in value for value in record.timeliness)
            ), None)
            if current is not None:
                attempt.update(
                    current_title=current.title,
                    current_implement_date=current.implement_date,
                )
            if matched:
                return replace(
                    article,
                    timeliness=matched.timeliness,
                    effectiveness=matched.effectiveness,
                    implement_date=matched.implement_date,
                    issue_date=matched.issue_date,
                    document_no=matched.document_no,
                )
        except PkulawNotFoundError:
            attempt["status"] = "not_found"
        except PkulawMcpError as exc:
            attempt.update(status="error", message=str(exc))
        return article

    def _article_result(self, request, trace, article):
        trace.status = LookupStatus.ARTICLE_FOUND
        trace.source_url = article.url
        trace.metadata.update(_article_metadata(article))
        evidence = ArticleEvidence(
            law_title=article.title,
            source_type=_evidence_source_type(article),
            article_no=article.article_no,
            article_text=article.article_text,
            version_label=_first(article.timeliness),
            version_status=_first(article.timeliness),
            effective_from=article.implement_date,
            source_metadata=trace.metadata,
            data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace)

    def _related_result(self, request, trace, top, related, message):
        trace.status = LookupStatus.RELEVANT_ARTICLES_FOUND
        trace.message = message
        trace.source_url = top.url
        trace.metadata.update(_article_metadata(top))
        trace.metadata["retrieval_method"] = "pkulaw_law_semantic"
        evidence = ArticleEvidence(
            law_title=top.title,
            source_type=_evidence_source_type(top),
            article_text="\n\n".join(_format_excerpt(x) for x in related),
            version_label=_first(top.timeliness),
            version_status=_first(top.timeliness),
            effective_from=top.implement_date,
            source_metadata=trace.metadata,
            related_articles=related,
            data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace)

    def _law_text_unavailable(self, request, trace, matched, records):
        trace.status = LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
        trace.message = "北大法宝已检索到该法规，但文书未注明条号，当前未取得条文全文"
        trace.source_url = matched.url
        trace.metadata.update(
            candidate_count=len(records),
            candidates=[_law_record_metadata(r) for r in records],
        )
        return LookupResult(
            trace.status, self._metadata_evidence(request, trace, matched), trace
        )

    def _metadata_evidence(self, request, trace, record):
        return ArticleEvidence(
            law_title=record.title,
            source_type=_evidence_source_type(record),
            article_no=request.article_no,
            article_text=None,
            version_label=_first(record.timeliness),
            version_status=_first(record.timeliness),
            effective_from=record.implement_date,
            source_metadata=_law_record_metadata(record),
            data_source=trace,
        )

    def _candidate_evidence(self, request, trace, article):
        return ArticleEvidence(
            law_title=article.title,
            source_type=_evidence_source_type(article),
            article_no=article.article_no,
            article_text=article.article_text,
            version_label=_first(article.timeliness),
            version_status=_first(article.timeliness),
            effective_from=article.implement_date,
            source_metadata=_article_metadata(article),
            data_source=trace.model_copy(update={"source_url": article.url}),
        )

    def _error(self, trace, exc):
        trace.status = _pkulaw_error_status(exc)
        trace.message = str(exc)
        return LookupResult(trace.status, None, trace)

    def _client(self):
        if self.client is None:
            client = PkulawMcpClient()
            self.client = CachedPkulawClient(client) if cache_enabled() else client
        return self.client


class StatuteLookupClient(Protocol):
    def get_article(self, title: str, article_no: str) -> PkulawArticle: ...

    def get_law_list(
        self, title: str = "", fulltext: str = ""
    ) -> list[PkulawLawRecord]: ...

    def search_law_articles(self, text: str) -> list[PkulawArticle]: ...

    def search_law_articles_for_article(
        self, title: str, article_no: str
    ) -> list[PkulawArticle]: ...

    def get_law_item_content(
        self, title: str, article_no: str
    ) -> PkulawArticle: ...

    def recognize_laws(self, text: str): ...


def _rank_articles(
    context_text: str, articles: list[PkulawArticle], limit: int = 3
) -> list[ArticleExcerpt]:
    rows = [
        {"text": a.article_text, "article_no": a.article_no, "article_key": i,
         "title": a.title, "version_key": a.implement_date, "source_url": a.url}
        for i, a in enumerate(articles)
    ]
    return retrieve_relevant_articles(context_text, rows, limit=limit)


def _versioned_query_title(title: str, hint: str | None) -> str:
    normalized = build_article_exact_title(title)
    if not hint or re.search(r"[（(].*(?:修正|修订|修改).*[）)]$", normalized):
        return normalized
    version = re.search(r"((?:19|20)\d{2})\s*年?\s*(修正|修订|修改)", hint)
    versioned = (
        f"{normalized}（{version.group(1)}{version.group(2)}）"
        if version else normalized
    )
    return cn_title_shape_variants(versioned)[0]


def _versioned_query_titles(title: str, hint: str | None) -> tuple[str, ...]:
    return cn_title_shape_variants(_versioned_query_title(title, hint))


def _match_requested_record(title: str, records):
    exact = next(
        (record for record in records if normalize_title(record.title) == normalize_title(title)),
        None,
    )
    return exact or match_law_record(title, records)


def _top_articles(
    context_text: str, articles: list[PkulawArticle], limit: int = 3
) -> list[PkulawArticle]:
    """保留与引文最相关的少量原始候选，避免逐候选远程富化。"""
    remaining = list(articles)
    selected = []
    for excerpt in _rank_articles(context_text, articles, limit=limit):
        match = next((
            (index, article)
            for index, article in enumerate(remaining)
            if article.title == excerpt.law_title
            and article.article_no == excerpt.article_no
            and article.article_text == excerpt.article_text
        ), None)
        if match is not None:
            index, article = match
            selected.append(article)
            remaining.pop(index)
    return selected


def _format_excerpt(excerpt: ArticleExcerpt) -> str:
    locator = excerpt.locator or excerpt.article_no
    prefix = f"{locator}　" if locator else ""
    return f"{prefix}{excerpt.article_text}"


def _pkulaw_error_status(exc: PkulawMcpError) -> LookupStatus:
    return (
        LookupStatus.SOURCE_NOT_CONFIGURED
        if isinstance(exc, PkulawNotConfiguredError)
        else LookupStatus.SOURCE_ERROR
    )


def _article_metadata(article: PkulawArticle) -> dict:
    return {**_law_record_metadata(article), "article_no": article.article_no}


def _law_record_metadata(record: PkulawLawRecord) -> dict:
    return {
        "title": record.title,
        "url": record.url,
        "category": record.category,
        "document_no": record.document_no,
        "issue_department": record.issue_department,
        "issue_date": record.issue_date,
        "implement_date": record.implement_date,
        "version_key": record.implement_date,
        "version_confirmed": bool(record.implement_date and "现行有效" in record.timeliness),
        "timeliness": record.timeliness,
        "effectiveness": record.effectiveness,
    }


def _evidence_source_type(record: PkulawLawRecord) -> str | None:
    """只保存权威数据源实际返回的文件类别，不根据标题猜测。"""
    return str(record.category[0]) if record.category else None


def _first(values: list[str]) -> Optional[str]:
    return values[0] if values else None


__all__ = ["PkulawFallbackSource"]
