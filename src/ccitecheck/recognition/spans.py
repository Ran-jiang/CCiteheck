"""在 claim.text 上确定性定位法条 mention 及其支配的引文范围。"""

from __future__ import annotations

import re

from ..domain.citation import (
    CitationLocator,
    CitationOccurrence,
    Claim,
)
from ..domain.legal_numbers import chinese_number_to_int
from ..domain.law_titles import cn_title_shape_variants
from .statutes import COMPACT_ARTICLE_RANGE_PATTERN

_ARTICLE_MENTION = re.compile(
    r"第[0-9○〇零一二三四五六七八九十百千两]+条(?:之[0-9○〇零一二三四五六七八九十]+)?"
)
_NUM = r"[0-9○〇零一二三四五六七八九十百千两]+"
_ARTICLE_ENUM = re.compile(rf"第(?P<values>{_NUM}(?:[、,，]{_NUM})+)条")
_ARTICLE_RANGE = re.compile(rf"第(?P<start>{_NUM})条(?:至|到)第?(?P<end>{_NUM})条")
_RELATIVE_ARTICLE = re.compile(r"前条|该条")
_PARAGRAPH_OCCURRENCE = re.compile(rf"(?:同条)?第(?P<number>{_NUM})款")
_ITEM_OCCURRENCE = re.compile(rf"第[（(]?(?P<number>{_NUM})[）)]?项")


def _normalize_article_no(value: str) -> str:
    match = re.fullmatch(
        r"第(?P<base>[0-9○〇零一二三四五六七八九十百千两]+)条(?:之(?P<suffix>[0-9○〇零一二三四五六七八九十]+))?",
        value.strip(),
    )
    if not match:
        return value.strip()
    base = chinese_number_to_int(match.group("base"))
    suffix = chinese_number_to_int(match.group("suffix")) if match.group("suffix") else None
    return f"{base}:{suffix or ''}" if base is not None else value.strip()


def _raw_mentions(text: str) -> list[tuple[int, int, str]]:
    result: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    for match in _ARTICLE_RANGE.finditer(text):
        start_no = chinese_number_to_int(match.group("start"))
        end_no = chinese_number_to_int(match.group("end"))
        if start_no is not None and end_no is not None and 0 < end_no - start_no <= 50:
            result.extend((match.start(), match.end(), f"{value}:") for value in range(start_no, end_no + 1))
            occupied.append(match.span())
    for match in COMPACT_ARTICLE_RANGE_PATTERN.finditer(text):
        start_no = int(match.group(1))
        end_no = int(match.group(2))
        if 0 < start_no <= end_no and end_no - start_no <= 50:
            result.extend((match.start(), match.end(), f"{value}:") for value in range(start_no, end_no + 1))
            occupied.append(match.span())
    for match in _ARTICLE_ENUM.finditer(text):
        if any(left <= match.start() < right for left, right in occupied):
            continue
        values = re.split(r"[、,，]", match.group("values"))
        for value in values:
            number = chinese_number_to_int(value)
            if number is not None:
                result.append((match.start(), match.end(), f"{number}:"))
        occupied.append(match.span())
    for match in _ARTICLE_MENTION.finditer(text):
        if not any(left <= match.start() < right for left, right in occupied):
            result.append((match.start(), match.end(), _normalize_article_no(match.group())))
    numeric = sorted(result)
    for match in _RELATIVE_ARTICLE.finditer(text):
        previous = next((item for item in reversed(numeric) if item[1] <= match.start()), None)
        if previous:
            base = int(previous[2].split(":", 1)[0])
            resolved = base - 1 if match.group() == "前条" else base
            if resolved > 0:
                result.append((match.start(), match.end(), f"{resolved}:"))
    return sorted(result)


def _suffix_mentions(
    text: str,
    start: int,
    end: int,
    paragraphs: list[str],
    items: list[str],
) -> list[tuple[int, int, str | None, str | None]]:
    """定位识别阶段已归入当前条号的款、项，并保留二者的从属关系。"""
    paragraph_set = set(paragraphs)
    item_set = set(items)
    tokens = sorted([
        *(
            (match.start(), match.end(), "paragraph", f"第{match.group('number')}款")
            for match in _PARAGRAPH_OCCURRENCE.finditer(text, start, end)
            if f"第{match.group('number')}款" in paragraph_set
        ),
        *(
            (match.start(), match.end(), "item", f"第{match.group('number')}项")
            for match in _ITEM_OCCURRENCE.finditer(text, start, end)
            if f"第{match.group('number')}项" in item_set
        ),
    ])
    explicit_paragraph = any(kind == "paragraph" for _, _, kind, _ in tokens)
    current_paragraph = (
        paragraphs[0]
        if not explicit_paragraph and len(paragraphs) == 1
        else None
    )
    pending_paragraph: tuple[int, int] | None = None
    result: list[tuple[int, int, str | None, str | None]] = []
    for token_start, token_end, kind, value in tokens:
        if kind == "paragraph":
            if pending_paragraph is not None:
                result.append((*pending_paragraph, current_paragraph, None))
            current_paragraph = value
            pending_paragraph = (token_start, token_end)
            continue
        result.append((token_start, token_end, current_paragraph, value))
        pending_paragraph = None
    if pending_paragraph is not None:
        result.append((*pending_paragraph, current_paragraph, None))
    return result


def locate_claim_article_spans(claim: Claim) -> None:
    """生成按原文出现次数建模的 citations，不回写 ArticleRef 临时状态。"""
    text = claim.text
    sources = list(getattr(claim.entities, "legal_sources", []))
    aliases: list[tuple[str, str]] = []
    for source in sources:
        if not source.title:
            continue
        for name in (source.title, source.canonical_title):
            if name:
                aliases.extend(
                    (variant, source.title) for variant in cn_title_shape_variants(name)
                )
    mentions = []
    for start, end, normalized in _raw_mentions(text):
        sentence_start = max(text.rfind(mark, 0, start) for mark in "。！？；;\n") + 1
        prefix = text[sentence_start:start]
        owners = [
            (prefix.rfind(alias), owner)
            for alias, owner in aliases
            if prefix.rfind(alias) >= 0
        ]
        owner = max(owners)[1] if owners else None
        mentions.append((start, end, normalized, owner))
    for source in sources:
        search_start = source.recognition.mention_span[1] if source.recognition.mention_span else 0
        for article in source.articles:
            if not article.raw_locator:
                continue
            start = text.find(article.raw_locator, search_start)
            if start >= 0:
                mentions.append((
                    start,
                    start + len(article.raw_locator),
                    _normalize_article_no(article.article),
                    source.title,
                ))
    mentions.sort()
    claimed_by: dict[str, set[str]] = {}
    for source in sources:
        for article in source.articles:
            claimed_by.setdefault(_normalize_article_no(article.article), set()).add(source.title)
    used: set[int] = set()
    located: list[tuple[int, int, object, object, tuple[int, int]]] = []
    occurrences: list[CitationOccurrence] = []
    for source in sources:
        for article in source.articles:
            expected = _normalize_article_no(article.article)
            candidates = [i for i, item in enumerate(mentions) if i not in used and item[2] == expected]
            all_mentions = [item for item in mentions if item[2] == expected]
            law_mention_span = source.recognition.mention_span
            if law_mention_span is not None:
                match_index = next((
                    i for i in candidates
                    if mentions[i][0] >= law_mention_span[1]
                ), None)
            elif len(all_mentions) == 1 and len(claimed_by.get(expected, ())) == 1:
                match_index = candidates[0] if candidates else None
            else:
                match_index = next((i for i in candidates if mentions[i][3] == source.title), None)
            if match_index is None:
                continue
            used.add(match_index)
            start, end, _, _ = mentions[match_index]
            previous_end = max(
                (item[1] for item in mentions if item[1] <= start),
                default=max(text.rfind(mark, 0, start) for mark in "。！？；;\n") + 1,
            )
            alias_start = (
                law_mention_span[0]
                if law_mention_span is not None
                else max(
                    (
                        text.rfind(alias, previous_end, start)
                        for alias, owner in aliases
                        if owner == source.title and text.rfind(alias, previous_end, start) >= 0
                    ),
                    default=start,
                )
            )
            if alias_start > 0 and text[alias_start - 1] == "《":
                alias_start -= 1
            located.append((start, end, article, source, (alias_start, end)))

    located.sort(key=lambda item: item[0])
    located_article_keys = {
        (id(item[3]), _normalize_article_no(item[2].article)) for item in located
    }
    for index, (article_start, article_end, article, source, citation_span) in enumerate(located):
        next_article_start = located[index + 1][0] if index + 1 < len(located) else len(text)
        same_article_refs = [
            item for item in source.articles
            if _normalize_article_no(item.article) == _normalize_article_no(article.article)
        ]
        paragraphs = list(dict.fromkeys(
            value for item in same_article_refs for value in item.paragraphs
        ))
        items = list(dict.fromkeys(
            value for item in same_article_refs for value in item.items
        ))
        suffixes = _suffix_mentions(
            text, article_end, next_article_start, paragraphs, items
        )
        if not suffixes:
            occurrences.append(CitationOccurrence(
                law_title=source.title,
                raw_law_title=source.title,
                locator=CitationLocator(article=article.article),
                role="direct",
                citation_span=citation_span,
                span_status="located",
            ))
            continue
        for suffix_index, (suffix_start, suffix_end, paragraph, item) in enumerate(suffixes):
            occurrences.append(CitationOccurrence(
                law_title=source.title,
                raw_law_title=source.title,
                locator=CitationLocator(
                    article=article.article,
                    paragraph=paragraph,
                    item=item,
                ),
                role=("direct" if suffix_index == 0 else "carry_forward"),
                citation_span=(
                    citation_span[0]
                    if suffix_index == 0
                    else suffix_start,
                    suffix_end,
                ),
                span_status="located",
            ))

    # 仅出现“同条第四款”“第五款”等承前定位时，没有字面条号；直接以款/项
    # 作为一次 citation，条号使用识别阶段从上下文补齐的 ArticleRef.article。
    for source in sources:
        for article in source.articles:
            if (id(source), _normalize_article_no(article.article)) in located_article_keys:
                continue
            suffixes = [*article.paragraphs, *article.items]
            if not suffixes:
                continue
            for suffix_start, suffix_end, paragraph, item in _suffix_mentions(
                text, 0, len(text), article.paragraphs, article.items
            ):
                occurrences.append(CitationOccurrence(
                    law_title=source.title,
                    raw_law_title=source.title,
                    locator=CitationLocator(
                        article=article.article,
                        paragraph=paragraph,
                        item=item,
                    ),
                    role="carry_forward",
                    citation_span=(suffix_start, suffix_end),
                    span_status="located",
                ))

    claim.entities.citations = sorted(
        occurrences,
        key=lambda item: item.citation_span[0] if item.citation_span else len(text),
    )
    for index, occurrence in enumerate(claim.entities.citations, 1):
        occurrence.mention_id = f"{claim.claim_id}:law:{index}"

__all__ = ["locate_claim_article_spans"]
