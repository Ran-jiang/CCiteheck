"""从本地 SQLite 数据库取得法规与条款证据的适配器。

本适配器只读取配置的本地法规库；引用未写明条号时执行确定性文本召回。
它返回证据和查询轨迹，不作通过或问题判定，也不调用远程回退服务。
"""

from __future__ import annotations

from pathlib import Path
from threading import Lock
import re

from ...infrastructure.database import (
    connect,
    find_current_article,
    find_law,
    get_structure_path_for_article,
    list_all_articles,
    list_article_versions,
    list_current_articles,
)

from ...domain.evidence import ArticleEvidence, LookupStatus, SourceTier, SourceTrace
from ..ranking import retrieve_relevant_articles
from .base import LookupRequest, LookupResult


class LocalSQLiteSource:
    """从本地数据库取得当前可用的最佳法规证据。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._articles = {}
        self._lock = Lock()

    def clear_cache(self) -> None:
        with self._lock:
            self._articles.clear()

    def current_articles(self, title: str) -> list[dict]:
        with self._lock:
            if title not in self._articles:
                with connect(self.db_path) as conn:
                    self._articles[title] = [dict(row) for row in list_current_articles(conn, title)]
            return self._articles[title]

    def all_articles(self, title: str) -> list[dict]:
        """该法规全部条文、全部已存版本（纠错反推的全版本池）。"""
        with self._lock:
            key = f"{title}#all-versions"
            if key not in self._articles:
                with connect(self.db_path) as conn:
                    self._articles[key] = [dict(row) for row in list_all_articles(conn, title)]
            return self._articles[key]

    def corpus_metadata(self, rows: list[dict]) -> dict:
        versions = {r["version_key"] for r in rows}
        version = next(iter(versions)) if len(versions) == 1 else None
        return {
            "local_article_count": len(rows),
            "version_key": version,
            "version_confirmed": True,
            "full_text_complete": False,
        }

    def lookup(self, request: LookupRequest) -> LookupResult:
        trace = SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="CCiteheck 本地 SQLite 法规库",
            status=LookupStatus.LAW_NOT_FOUND,
        )
        if not self.db_path.exists():
            trace.status = LookupStatus.SOURCE_NOT_CONFIGURED
            trace.message = f"SQLite database not found: {self.db_path}"
            return LookupResult(trace.status, None, trace)

        with connect(self.db_path) as conn:
            if request.article_no:
                article = _versioned_article(conn, request)
                if article:
                    trace.metadata = (
                        {
                            "version_key": article["version_key"],
                            "version_label": article["version_label"],
                            "version_confirmed": True,
                            "full_text_complete": False,
                        }
                        if request.version_hint and request.version_hint != "current"
                        else self.corpus_metadata(self.current_articles(request.law_title))
                    )
                    if request.version_hint and request.version_hint != "current":
                        current = find_current_article(
                            conn, request.law_title, request.article_no or ""
                        )
                        if current and current["version_key"] != article["version_key"]:
                            current_title = current["title"]
                            current_label = current["version_label"] or ""
                            if re.search(r"修正|修订|修改", current_label):
                                current_title = f"{current_title}（{current_label}）"
                            trace.metadata.update(
                                current_title=current_title,
                                current_implement_date=(
                                    current["effective_from"] or current["effective_at"]
                                ),
                            )
                    trace.status = LookupStatus.ARTICLE_FOUND
                    trace.source_name = article["source_name"] or trace.source_name
                    trace.source_url = article["source_url"] or trace.metadata.get("source_url") or find_law(conn, request.law_title)["source_url"]
                    trace.fetched_at = article["source_fetched_at"] or trace.fetched_at
                    evidence = ArticleEvidence(
                        law_title=article["title"],
                        source_type=article["source_type"],
                        article_no=article["article_no"],
                        article_text=article["text"],
                        version_label=article["version_label"] or article["timeliness"],
                        version_status=article["version_status"] or article["law_status"],
                        effective_from=article["effective_from"] or article["effective_at"],
                        source_metadata={
                            "version_key": article["version_key"],
                            "timeliness": article["timeliness"],
                            "effectiveness": article["effectiveness"],
                            "issued_at": article["issued_at"],
                            "effective_from": article["effective_from"],
                            "effective_to": article["effective_to"],
                            "effective_at": article["effective_at"],
                        },
                        structure_path=get_structure_path_for_article(
                            conn, int(article["article_id"])
                        ),
                        data_source=trace,
                    )
                    return LookupResult(trace.status, evidence, trace)

            law = find_law(conn, request.law_title)
            if law:
                trace.source_url = law["source_url"]
                if not request.article_no:
                    related_articles = (
                        retrieve_relevant_articles(
                            request.query_text or request.context_text,
                            self.current_articles(request.law_title),
                            limit=8,
                        )
                        if not request.existence_only else []
                    )
                    if related_articles:
                        trace.metadata = self.corpus_metadata(
                            self.current_articles(request.law_title)
                        )
                        trace.source_url = trace.source_url or trace.metadata.get("source_url")
                        trace.status = LookupStatus.RELEVANT_ARTICLES_FOUND
                        trace.message = "文书未注明条号，已从本地全文召回相关条款"
                        evidence = ArticleEvidence(
                            law_title=law["title"],
                            source_type=law["source_type"],
                            article_text="\n\n".join(
                                f"{item.article_no}　{item.article_text}"
                                for item in related_articles
                            ),
                            version_status=law["status"],
                            source_metadata={
                                "authority": law["authority"],
                                "category": law["category"],
                                "retrieval_method": "local_article_bm25",
                            },
                            related_articles=related_articles,
                            data_source=trace,
                        )
                        return LookupResult(trace.status, evidence, trace)
                trace.status = (
                    LookupStatus.LAW_FOUND_ARTICLE_MISSING
                    if request.article_no
                    else LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
                )
                trace.metadata = self.corpus_metadata(
                    self.current_articles(request.law_title)
                )
                trace.source_url = trace.source_url or trace.metadata.get("source_url")
                trace.message = (
                    "本地库已收录该法规，但无可用条文全文"
                    if not request.article_no
                    else f"本地库已收录该法规，但未找到{request.article_no}"
                )
                evidence = ArticleEvidence(
                    law_title=law["title"],
                    source_type=law["source_type"],
                    article_no=request.article_no,
                    article_text=None,
                    version_status=law["status"],
                    source_metadata={
                        "authority": law["authority"],
                        "category": law["category"],
                    },
                    data_source=trace,
                )
                return LookupResult(trace.status, evidence, trace)

        trace.message = "本地法规库未收录该法规"
        return LookupResult(trace.status, None, trace)


def _versioned_article(conn, request: LookupRequest):
    hint = request.version_hint or ""
    if not hint or hint == "current":
        return find_current_article(conn, request.law_title, request.article_no or "")
    year = next(iter(re.findall(r"(?:19|20)\d{2}", hint)), None)
    versions = list_article_versions(conn, request.law_title, request.article_no or "")
    if year:
        matched = [row for row in versions if year in "".join(str(row[key] or "") for key in (
            "version_key", "version_label", "issued_at", "effective_from"
        ))]
        if matched:
            return matched[0]
        return find_current_article(
            conn, request.law_title, request.article_no or "", as_of=f"{year}-12-31"
        )
    return None


__all__ = ["LocalSQLiteSource"]
