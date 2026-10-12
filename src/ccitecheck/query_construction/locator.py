"""原始条款定位符归一化。"""

from __future__ import annotations

import re

from ..domain.legal_numbers import chinese_number_to_int
from ..domain.queries import NormalizedLocator

_NUMBER = re.compile(r"[〇零一二两三四五六七八九十百千万0-9]+")


def _number(value: str | None) -> int | None:
    match = _NUMBER.search(value or "")
    return chinese_number_to_int(match.group()) if match else None


def normalize_locator(
    article_raw: str | None,
    paragraphs_raw: list[str] | None = None,
    items_raw: list[str] | None = None,
    structures_raw: list[str] | None = None,
) -> NormalizedLocator | None:
    paragraph = (paragraphs_raw or [None])[0]
    item = (items_raw or [None])[0]
    if not any((article_raw, paragraph, item, structures_raw)):
        return None
    return NormalizedLocator(
        article_number=_number(article_raw),
        article_raw=article_raw,
        paragraph_number=_number(paragraph),
        paragraph_raw=paragraph,
        item_number=_number(item),
        item_raw=item,
        structure_labels=list(structures_raw or []),
    )


__all__ = ["normalize_locator"]
