"""法规引用识别结果的跨字段不变量。"""

from __future__ import annotations

from .citation import Claim, ClaimType, LegalSource
from .law_titles import cn_title_shape_variants


class CitationIntegrityError(ValueError):
    """法规识别结构无法安全进入溯源阶段。"""


def ensure_claim_citation_integrity(claim: Claim) -> None:
    """不变量不成立时中止，避免下游静默遗漏引用。"""
    violations = validate_claim_citation_integrity(claim)
    if violations:
        raise CitationIntegrityError(
            f"claim {claim.claim_id} 的法规引用结构不完整："
            + "；".join(violations)
        )


def validate_claim_citation_integrity(claim: Claim) -> list[str]:
    """检查法源、引用出现和核验目标之间的一致性。"""
    if claim.claim_type != ClaimType.LEGAL_SOURCE_CLAIM:
        return []

    text = claim.text
    sources = claim.entities.legal_sources
    citations = claim.entities.citations
    violations: list[str] = []

    source_by_name = legal_source_alias_index(sources)
    ambiguous_names = {
        name
        for source in sources
        for name in _source_names(source)
        if sum(name in _source_names(item) for item in sources) > 1
    }
    violations.extend(
        f"法名 {name!r} 同时指向多个法源"
        for name in sorted(ambiguous_names)
    )
    for source in sources:
        _validate_span(text, source.recognition.mention_span, "recognition.mention_span", violations)
    for index, mention in enumerate(claim.entities.unresolved_legal_mentions):
        _validate_span(
            text,
            mention.resolution_anchor_span,
            f"unresolved_legal_mentions[{index}].resolution_anchor_span",
            violations,
        )

    citations_by_source: dict[int, list] = {id(source): [] for source in sources}
    for index, citation in enumerate(citations):
        label = f"citations[{index}]"
        source = source_by_name.get(citation.law_title)
        if source is None:
            violations.append(f"{label}.law_title 未对应 legal_sources: {citation.law_title!r}")
            continue
        citations_by_source[id(source)].append(citation)
        article = next(
            (item for item in source.articles if item.article == citation.locator.article),
            None,
        )
        if article is None:
            violations.append(
                f"{label}.locator.article 未对应 {source.title} 的 articles: "
                f"{citation.locator.article!r}"
            )

        _validate_span(text, citation.citation_span, f"{label}.citation_span", violations)
        if citation.span_status == "located" and citation.citation_span is None:
            violations.append(f"{label}.span_status=located 但缺少 citation_span")
        if article is not None:
            if (
                citation.locator.paragraph is not None
                and citation.locator.paragraph not in article.paragraphs
            ):
                violations.append(
                    f"{label}.locator.paragraph 未对应 ArticleRef.paragraphs"
                )
            if (
                citation.locator.item is not None
                and citation.locator.item not in article.items
            ):
                violations.append(
                    f"{label}.locator.item 未对应 ArticleRef.items"
                )

    for source in sources:
        source_citations = citations_by_source[id(source)]
        for article in source.articles:
            matches = [
                citation for citation in source_citations
                if citation.locator.article == article.article
            ]
            if not matches:
                violations.append(
                    f"{source.title}{article.article} 已识别但缺少 citation occurrence"
                )
                continue
            for paragraph in article.paragraphs:
                if not any(item.locator.paragraph == paragraph for item in matches):
                    violations.append(
                        f"{source.title}{article.article}{paragraph} 已识别但缺少 citation occurrence"
                    )
            for item in article.items:
                if not any(citation.locator.item == item for citation in matches):
                    violations.append(
                        f"{source.title}{article.article}{item} 已识别但缺少 citation occurrence"
                    )

    return violations


def legal_source_alias_index(sources: list[LegalSource]) -> dict[str, LegalSource]:
    """以原文法名和规范法名共同建立唯一法源索引。"""
    result: dict[str, LegalSource] = {}
    for source in sources:
        for name in _source_names(source):
            result.setdefault(name, source)
    return result


def _source_names(source: LegalSource) -> set[str]:
    return {
        variant
        for name in (source.title, source.canonical_title)
        if name
        for variant in cn_title_shape_variants(name)
    }


def _validate_span(
    text: str,
    span: tuple[int, int] | None,
    label: str,
    violations: list[str],
) -> None:
    if span is None:
        return
    start, end = span
    if start < 0 or end < start or end > len(text):
        violations.append(f"{label} 越界: {span}, text_length={len(text)}")


__all__ = [
    "CitationIntegrityError",
    "ensure_claim_citation_integrity",
    "legal_source_alias_index",
    "validate_claim_citation_integrity",
]
