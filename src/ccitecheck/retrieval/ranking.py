"""以单条法条为文档执行 BM25 稀疏召回。"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping

import jieba

from ..domain.evidence import ArticleExcerpt

BOOK_TITLE_RE = re.compile(r"《[^》]+》")
TOKEN_RE = re.compile(r"[\u3400-\u9fffA-Za-z0-9]+")


def retrieve_relevant_articles(
    context_text: str,
    articles: Iterable[Mapping],
    limit: int = 3,
) -> list[ArticleExcerpt]:
    rows = list(articles)
    query_terms = _tokens(BOOK_TITLE_RE.sub("", context_text))
    if not query_terms or not rows:
        return []

    article_terms = [_tokens(row["text"]) for row in rows]
    document_frequency = Counter(
        term for terms in article_terms for term in set(terms)
    )
    article_count = len(rows)
    average_length = sum(map(len, article_terms)) / article_count
    query_frequency = Counter(query_terms)
    ranked = []
    for row, terms in zip(rows, article_terms):
        term_frequency = Counter(terms)
        shared = query_frequency.keys() & term_frequency.keys()
        if not shared:
            continue
        length_norm = 1 - 0.75 + 0.75 * len(terms) / max(1, average_length)
        score = 0.0
        for term in shared:
            frequency = term_frequency[term]
            inverse_frequency = math.log(
                1 + (article_count - document_frequency[term] + 0.5)
                / (document_frequency[term] + 0.5)
            )
            score += (
                inverse_frequency
                * frequency * 2.2 / (frequency + 1.2 * length_norm)
                * min(2, query_frequency[term])
            )
        ranked.append((score, row))

    ranked.sort(key=lambda item: (-item[0], item[1]["article_key"]))
    return [
        ArticleExcerpt(
            law_title=_value(row, "title"),
            version_key=_value(row, "version_key"),
            source_url=_value(row, "source_url"),
            article_no=str(_value(row, "article_no") or ""),
            locator=_value(row, "locator") or _value(row, "article_no") or None,
            locator_type=_value(row, "locator_type") or "article",
            article_text=row["text"],
            relevance_score=round(score, 6),
        )
        for score, row in ranked[:limit]
        if score > 0
    ]


def _value(row: Mapping, key: str):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _tokens(text: str) -> list[str]:
    return [
        token.lower()
        for token in jieba.cut_for_search(text)
        if TOKEN_RE.fullmatch(token) and len(token.strip()) > 1
    ]
