"""把权威条文文本解析为条、款、项结构。"""

from __future__ import annotations

import re

from ...domain.statute_results import StructuredArticle, StructuredItem, StructuredParagraph
from ...domain.legal_numbers import chinese_number_to_int, int_to_chinese_number

_ARTICLE_HEADING = re.compile(
    r"^\s*第[〇零一二三四五六七八九十百千万两0-9]+条"
    r"(?:之[〇零一二三四五六七八九十百千万两0-9]+)?[\s　]*"
)
_ITEM_MARKER = re.compile(r"[（(]([〇零一二三四五六七八九十百千万两0-9]+)[）)]")
_EXPLICIT_PARAGRAPH = re.compile(r"^\s*(\d+)\s*[.．]\s*")


def parse_article_structure(
    article_no: str, article_text: str, *, trust_single_paragraph: bool = False
) -> StructuredArticle | None:
    """解析保留自然段边界的条文；空文本或孤立项结构返回 None。

    trust_single_paragraph=True 时，即便文本仅一行也认定款边界可靠（用于本地
    精编库：真实多款均保留换行，单行即确为一款，可据此判定超范围款号）。"""
    heading = _ARTICLE_HEADING.match(article_text)
    body_start = heading.end() if heading else 0
    lines = []
    for match in re.finditer(r"[^\r\n]+", article_text[body_start:]):
        if not match.group().strip():
            continue
        left = body_start + match.start() + len(match.group()) - len(match.group().lstrip())
        right = body_start + match.end() - len(match.group()) + len(match.group().rstrip())
        lines.append((left, right))
    if not lines:
        return None
    ranges: list[tuple[int, int]] = []
    for left, right in lines:
        if _ITEM_MARKER.match(article_text[left:right]):
            if not ranges:
                return None
            ranges[-1] = (ranges[-1][0], right)
        else:
            ranges.append((left, right))
    paragraphs = []
    for index, (left, right) in enumerate(ranges, 1):
        text = article_text[left:right]
        marker = _ITEM_MARKER.search(text)
        explicit = _EXPLICIT_PARAGRAPH.match(text)
        paragraph_index = int(explicit.group(1)) if explicit else index
        paragraphs.append(StructuredParagraph(
            paragraph_no=f"第{int_to_chinese_number(paragraph_index)}款", text=text,
            items=_parse_items(text), introduction=text[:marker.start()] if marker else "",
        ))
    return StructuredArticle(
        article_no=article_no,
        raw_text=article_text,
        paragraph_boundaries_reliable=len(lines) > 1 or trust_single_paragraph,
        paragraphs=paragraphs,
    )


def locator_ordinal(value: str, suffix: str) -> int | None:
    match = re.fullmatch(
        rf"第?([〇零一二三四五六七八九十百千万两0-9]+){suffix}", value.strip()
    )
    if not match:
        return None
    token = match.group(1)
    try:
        return int(token) if token.isdigit() else chinese_number_to_int(token)
    except ValueError:
        return None


def _parse_items(text: str) -> list[StructuredItem]:
    matches = list(_ITEM_MARKER.finditer(text))
    return [
        StructuredItem(
            item_no=f"第{int_to_chinese_number(_number(match.group(1)))}项",
            text=text[match.start():matches[index + 1].start()].strip()
            if index + 1 < len(matches)
            else text[match.start():].strip(),
        )
        for index, match in enumerate(matches)
    ]


def _number(value: str) -> int:
    return int(value) if value.isdigit() else chinese_number_to_int(value)



__all__ = ["locator_ordinal", "parse_article_structure"]
