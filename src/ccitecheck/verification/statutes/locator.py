"""候选召回与证据确认分离；分数永远不能单独产生确定纠错。"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from ...domain.evidence import ArticleEvidence, SourceTier
from ...domain.legal_numbers import chinese_number_to_int
from ...domain.statute_results import StatuteLocationCandidate, StatuteLocationResolution, StatuteLocator
from ...infrastructure.database import normalize_article_key
from .structure import parse_article_structure

_NON_TEXT = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]")
_NUMBERS = re.compile(r"[零〇一二两三四五六七八九十百千]+|[0-9]+")


def _normalize(value: str) -> str:
    def number(match):
        parsed = chinese_number_to_int(match.group())
        return str(parsed) if parsed is not None else match.group()
    return _NON_TEXT.sub("", _NUMBERS.sub(number, value))


def assertion_fragments(text: str) -> list[tuple[int, int]]:
    """原始半开区间；合并相邻片段由规范化全文匹配同时覆盖。"""
    return [m.span() for m in re.finditer(r"[^。；;，,：:！？\n]+", text) if _normalize(m.group())]


def _match(quote: str, text: str) -> tuple[bool, float]:
    normalized = _normalize(quote)
    body = re.sub(r"[（(][一二三四五六七八九十0-9]+[）)]", "", text)
    target = _normalize(body)
    if not normalized or not target:
        return False, 0.0
    blocks = SequenceMatcher(None, normalized, target, autojunk=False).get_matching_blocks()
    coverage = sum(b.size for b in blocks) / len(normalized)
    # 短的刑种、金额或共享措辞只能作为待核查候选。
    if max(b.size for b in blocks) < 10 or coverage < .7:
        return False, coverage
    fragments = [_normalize(quote[a:b]) for a, b in assertion_fragments(quote)]
    boundaries = {0}
    length = 0
    for a, b in assertion_fragments(body):
        length += len(_normalize(body[a:b]))
        boundaries.add(length)
    whole = target.find(normalized)
    if whole in boundaries and whole + len(normalized) in boundaries:
        return not _material_gap(target[:whole] + target[whole + len(normalized):]), coverage
    position = 0
    for fragment in fragments:
        found = target.find(fragment, position)
        if found < 0:
            return False, coverage
        # 防止把“不应当……”内部的“应当……”当作相同规范。
        if found and target[found - 1] in "不未无非" and not fragment.startswith(target[found - 1]):
            return False, coverage
        if _material_gap(target[position:found]):
            return False, coverage
        if found not in boundaries or found + len(fragment) not in boundaries:
            return False, coverage
        position = found + len(fragment)
    return not _material_gap(target[position:]), coverage


def _material_gap(text: str) -> bool:
    # 保守阻止省略实质限定；存在这些差异时交给带证据跨度的语义核查。
    return bool(re.search(r"如果|除非|只有|但是|不|未|无|非|禁止|情节|数额|过错|故意|过失|违约|违反|\d+(?:年|月|日|元|倍|人)|的$", text))


def supports_assertion(quote: str, text: str) -> bool:
    return _match(quote, text)[0]


def _supports_reordered_item(quote: str, introduction: str, item: str) -> bool:
    """允许“项的条件＋上位款结论”的常见转述顺序。"""
    fragments = [_normalize(quote[a:b]).removesuffix("的") for a, b in assertion_fragments(quote)]
    intro = _normalize(introduction)
    body = _normalize(re.sub(r"^[（(][^）)]+[）)]", "", item))
    if len(fragments) < 2:
        return False
    item_hits = [part for part in fragments if len(part) >= 2 and part in body]
    intro_hits = [part for part in fragments if len(part) >= 4 and part in intro]
    return bool(item_hits and intro_hits and all(
        part in body or part in intro for part in fragments
    ))


def _matches_distinctive_fragment(quote: str, text: str) -> bool:
    fragments = [_normalize(quote[a:b]).removesuffix("的") for a, b in assertion_fragments(quote)]
    target = _normalize(text)
    return bool(fragments and len(fragments[0]) >= 6 and fragments[0] in target)


def resolve_location_candidates(
    claim_text: str,
    evidence: list[ArticleEvidence],
    *,
    cited_article_no: str | None = None,
) -> StatuteLocationResolution:
    candidates: list[StatuteLocationCandidate] = []
    for article in evidence:
        if not article.article_no or not article.article_text:
            continue
        structure = parse_article_structure(
            article.article_no, article.article_text,
            trust_single_paragraph=article.data_source.tier == SourceTier.LOCAL_SQLITE,
        )
        if structure is None:
            continue
        offset = 0
        for paragraph in structure.paragraphs:
            paragraph_start = article.article_text.find(paragraph.text, offset)
            if paragraph_start < 0:
                continue
            offset = paragraph_start + len(paragraph.text)
            units = []
            for item in paragraph.items:
                item_start = article.article_text.find(item.text, paragraph_start, offset)
                spans = [(paragraph_start, paragraph_start + len(paragraph.introduction))] if paragraph.introduction else []
                spans.append((item_start, item_start + len(item.text)))
                units.append((item.item_no, spans, item.text))
            # 整款作为候选允许同款多个项共同支持，但不跨款拼接。
            units.append((None, [(paragraph_start, offset)], paragraph.text))
            item_supported = False
            for item_no, spans, unit_text in units:
                if item_no is None and item_supported:
                    continue
                text = "".join(article.article_text[a:b] for a, b in spans)
                supported, coverage = _match(claim_text, text)
                if item_no and not supported and _supports_reordered_item(
                    claim_text, paragraph.introduction, unit_text,
                ):
                    supported, coverage = True, max(coverage, _match(
                        claim_text, unit_text + paragraph.introduction,
                    )[1])
                rank_confirmed = article.source_metadata.get("article_rank_confirmed") is True
                if not supported and coverage < .7 and not rank_confirmed:
                    continue
                level = (
                    "item" if item_no
                    else "paragraph" if structure.paragraph_boundaries_reliable
                    and len(structure.paragraphs) > 1
                    else "article"
                )
                candidate = StatuteLocationCandidate(
                    locator=StatuteLocator(article_no=article.article_no,
                        paragraph_no=paragraph.paragraph_no if level != "article" else None, item_no=item_no),
                    text=text, source_url=article.data_source.source_url,
                    confirmed_level=level if supported else "article" if rank_confirmed else None,
                    evidence_spans=spans, coverage=coverage, supported=supported,
                )
                item_supported |= candidate.supported and item_no is not None
                if candidate.supported or candidate.coverage >= .7 or rank_confirmed:
                    candidates.append(candidate)
    unique = {}
    for candidate in candidates:
        loc = candidate.locator
        unique.setdefault((normalize_article_key(loc.article_no or ""), loc.paragraph_no, loc.item_no), candidate)
    candidates = list(unique.values())
    # 候选顺序与池子构造顺序解耦，保证结果稳定（最优在前）。
    candidates.sort(key=lambda c: (
        -c.coverage,
        normalize_article_key(c.locator.article_no or ""),
        c.locator.paragraph_no or "",
        c.locator.item_no or "",
    ))
    supported = [c for c in candidates if c.supported]
    supported_articles = {
        normalize_article_key(c.locator.article_no or "") for c in supported
    }
    if len(supported) == 1 and len(supported_articles) == 1:
        return StatuteLocationResolution(status="resolved", candidates=supported)
    if cited_article_no and not supported:
        cited_key = normalize_article_key(cited_article_no)
        strong_paragraphs = [
            candidate for candidate in candidates
            if candidate.locator.paragraph_no
            and normalize_article_key(candidate.locator.article_no or "") == cited_key
            and candidate.coverage >= .85
            and _matches_distinctive_fragment(claim_text, candidate.text)
        ]
        if len(strong_paragraphs) == 1:
            return StatuteLocationResolution(status="resolved", candidates=[
                strong_paragraphs[0].model_copy(update={
                    "confirmed_level": "paragraph",
                    "supported": True,
                })
            ])
    if cited_article_no and len(supported_articles) == 1:
        article_no = supported[0].locator.article_no
        if normalize_article_key(article_no or "") != normalize_article_key(cited_article_no):
            return StatuteLocationResolution(status="resolved", candidates=[
                supported[0].model_copy(update={
                    "locator": StatuteLocator(article_no=article_no),
                    "confirmed_level": "article",
                    "supported": False,
                })
            ])
    if cited_article_no and not supported:
        by_article: dict[str, StatuteLocationCandidate] = {}
        for candidate in candidates:
            key = normalize_article_key(candidate.locator.article_no or "")
            if key not in by_article or candidate.coverage > by_article[key].coverage:
                by_article[key] = candidate
        ranked = sorted(by_article.values(), key=lambda item: item.coverage, reverse=True)
        if ranked:
            distinctive = [
                candidate for candidate in ranked
                if _matches_distinctive_fragment(claim_text, candidate.text)
            ]
            best = distinctive[0] if len(distinctive) == 1 else ranked[0]
            strong = best.coverage >= .85 or best.confirmed_level == "article"
            separated = (
                len(distinctive) == 1
                or len(ranked) == 1
                or best.coverage - ranked[1].coverage >= .1
            )
            if strong and separated and normalize_article_key(
                best.locator.article_no or ""
            ) != normalize_article_key(cited_article_no):
                return StatuteLocationResolution(status="resolved", candidates=[
                    best.model_copy(update={
                        "locator": StatuteLocator(article_no=best.locator.article_no),
                        "confirmed_level": "article",
                    })
                ])
    return StatuteLocationResolution(status="candidates_pending" if candidates else "not_found", candidates=candidates)
__all__ = ["resolve_location_candidates", "supports_assertion"]
