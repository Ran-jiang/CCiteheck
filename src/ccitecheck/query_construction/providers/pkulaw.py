"""用权威条文候选严格解析未知裸法名。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ...infrastructure.database import normalize_article_key, normalize_title
from ...domain.law_titles import cn_title_shape_variants
from ..matching import match_law_record


class LawArticle(Protocol):
    title: str
    article_no: str
    url: str | None


class LawRecord(Protocol):
    title: str
    url: str | None


class LawNameClient(Protocol):
    def search_law_articles(self, text: str) -> list[LawArticle]: ...
    def get_law_list(self, title: str = "", fulltext: str = "") -> list[LawRecord]: ...


@dataclass(frozen=True)
class ResolvedLawName:
    surface_title: str
    canonical_title: str
    source_url: str | None = None


def resolve_law_name(
    client: LawNameClient,
    *,
    raw_left_window: str,
    article_no: str,
    context_text: str,
) -> ResolvedLawName | None:
    """仅接受条号一致、法名变体为原文后缀、且法规存在的唯一候选。"""
    cited_key = normalize_article_key(article_no)
    raw = normalize_title(raw_left_window)
    articles = client.search_law_articles(
        f"识别以下文字引用的具体法律和条文：{context_text}"[:500]
    )
    accepted: dict[str, tuple[LawArticle, str]] = {}
    for article in articles:
        if normalize_article_key(article.article_no) != cited_key:
            continue
        surface = next(
            (variant for variant in _safe_title_variants(article.title) if raw.endswith(variant)),
            None,
        )
        if surface is None:
            continue
        accepted.setdefault(article.title, (article, surface))
    if len(accepted) != 1:
        return None

    canonical_title, (article, surface) = next(iter(accepted.items()))
    laws = client.get_law_list(title=canonical_title)
    law = match_law_record(canonical_title, laws)
    if law is None:
        return None
    return ResolvedLawName(
        surface_title=surface,
        canonical_title=law.title,
        source_url=article.url or law.url,
    )


def _safe_title_variants(title: str) -> list[str]:
    normalized = normalize_title(title)
    return sorted(cn_title_shape_variants(normalized), key=len, reverse=True)


__all__ = ["ResolvedLawName", "resolve_law_name"]
