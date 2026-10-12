"""按不同检索能力构造简洁的法律信息查询参数。"""

from __future__ import annotations

import re

from ...domain.law_titles import canonical_cn_title_shape
from ...infrastructure.database import normalize_title, strip_version_annotation


_CASE_NUMBER = re.compile(
    r"[（(〔]\d{4}[）)〕][^，。；\s]{2,24}?号(?:之[一二三四五六七八九十]+)?"
)
_CITATION = re.compile(r"《[^》]{1,100}》(?:第[^，。；\s]{1,30}条)?")
_PUNCTUATION = re.compile(r"[，。；：、！？!?（）()【】\[\]“”\"'\s]+")
_LEADING_NOISE = re.compile(
    r"^(?:根据|依据|依照|按照|参照|参见|援引|该案|本案|法院认为|裁判认为|规定|指出)+"
)
_TRAILING_NOISE = re.compile(r"(?:规定|认为|指出|显示|可见|参照|参见)$")
_EMPTY_CONNECTORS = {"中", "在", "在中", "于", "其中", "对此"}


def build_law_title_query(title: str) -> str:
    return canonical_cn_title_shape(strip_version_annotation(normalize_title(title)))


def build_article_exact_title(title: str) -> str:
    # 精确条文查询必须保留“（2020修正）”等版本注记。删除版本后，
    # 同名历史版本会进入候选并可能污染条文与时效信息。
    return canonical_cn_title_shape(normalize_title(title))


def build_article_semantic_fallback_query(title: str, article_no: str) -> str:
    return f"《{build_article_exact_title(title)}》{article_no}的条文内容"


def has_substantive_content(context_text: str, law_title: str) -> bool:
    normalized_title = build_law_title_query(law_title)
    return (
        len(
            "".join(
                _clean_phrases(context_text, excluded=(law_title, normalized_title))
            )
        )
        >= 10
    )


def build_law_fulltext_query(context_text: str, law_title: str) -> str:
    return " ".join(_keyword_phrases(context_text, excluded=(law_title,)))


def build_law_semantic_query(context_text: str, law_title: str) -> str:
    title = build_law_title_query(law_title)
    proposition = _compact_context(context_text, excluded=(law_title,))
    return f"在《{title}》中检索与以下引用表述最相关的具体条文：{proposition}"[:500]


def build_case_keyword_query(
    case_name: str | None,
    context_text: str,
    court: str | None = None,
) -> tuple[str, str]:
    title = (case_name or "").strip()
    excluded = tuple(value for value in (case_name, court) if value)
    return title, " ".join(_keyword_phrases(context_text, excluded=excluded))


def build_case_semantic_query(
    case_name: str | None,
    context_text: str,
    court: str | None = None,
) -> str:
    parts = ["检索能够核验以下直接案例引用的司法案例"]
    if case_name:
        parts.append(f"案例线索：{case_name}")
    if court:
        parts.append(f"法院：{court}")
    parts.append(f"文书表述：{_compact_context(context_text, excluded=())}")
    return "；".join(parts)[:500]


def _keyword_phrases(
    text: str,
    excluded: tuple[str | None, ...],
    max_terms: int = 4,
) -> list[str]:
    terms: list[str] = []
    for term in _clean_phrases(text, excluded):
        if len(term) < 2 or term in terms:
            continue
        terms.append(term[:24])
        if len(terms) == max_terms:
            break
    return terms


def _clean_phrases(text: str, excluded: tuple[str | None, ...]) -> list[str]:
    cleaned = _remove_citations(text)
    for value in excluded:
        if value:
            cleaned = cleaned.replace(value, "")
    phrases: list[str] = []
    for raw in _PUNCTUATION.split(cleaned):
        phrase = _TRAILING_NOISE.sub("", _LEADING_NOISE.sub("", raw)).strip()
        if phrase and phrase not in _EMPTY_CONNECTORS:
            phrases.append(phrase)
    return phrases


def _compact_context(text: str, excluded: tuple[str | None, ...]) -> str:
    terms = _keyword_phrases(text, excluded, max_terms=6)
    return "；".join(terms) or _remove_citations(text).strip()[:200]


def _remove_citations(text: str) -> str:
    return _CASE_NUMBER.sub("", _CITATION.sub("", text))
