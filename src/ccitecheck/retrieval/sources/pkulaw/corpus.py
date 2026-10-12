"""把 law_recognition 返回的单部法规文本切成可检索语料。"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


_NUMBER = r"〇零一二两三四五六七八九十百千万0-9"
_ARTICLE = re.compile(
    rf"(?m)^[\t \u3000]*(?P<label>第[{_NUMBER}]+条(?:之[{_NUMBER}]+)?)(?!第)[\t \u3000]*"
)
_SECTION = re.compile(
    rf"^(?:第[{_NUMBER}]+(?:编|章|节)|[{_NUMBER}]+、|[（(][{_NUMBER}]+[）)])"
)


@dataclass(frozen=True)
class CorpusChunk:
    text: str
    locator: str
    locator_type: Literal["article", "paragraph"]
    sequence: int
    article_no: str = ""


@dataclass(frozen=True)
class ParsedCorpus:
    chunks: tuple[CorpusChunk, ...]
    structure: Literal["article", "paragraph", "empty"]
    structurally_valid: bool
    warnings: tuple[str, ...] = ()


def split_recognized_fulltext(fulltext: str) -> ParsedCorpus:
    """优先按原文条号切分；没有条号时保留章节/段落定位。"""
    text = fulltext.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ParsedCorpus((), "empty", False, ("empty_fulltext",))

    matches = list(_ARTICLE.finditer(text))
    if matches:
        chunks = []
        labels = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[match.end():end].strip()
            label = match.group("label")
            if body:
                chunks.append(CorpusChunk(
                    text=body,
                    locator=label,
                    locator_type="article",
                    sequence=index + 1,
                    article_no=label,
                ))
                labels.append(label)
        warnings = []
        if text[:matches[0].start()].strip():
            warnings.append("preamble_not_indexed")
        if len(labels) != len(set(labels)):
            warnings.append("duplicate_article_heading")
        valid = bool(chunks) and len(chunks) == len(matches) and len(labels) == len(set(labels))
        return ParsedCorpus(tuple(chunks), "article", valid, tuple(warnings))

    blocks = [item.strip() for item in re.split(r"\n\s*\n+", text) if item.strip()]
    if len(blocks) == 1:
        lines = [item.strip() for item in text.splitlines() if item.strip()]
        if len(lines) > 1:
            blocks = lines
    chunks = []
    section = ""
    paragraph_no = 0
    for block in blocks:
        compact = " ".join(block.split())
        if _is_section_heading(compact):
            section = compact
            continue
        paragraph_no += 1
        locator = f"{section} · 段落{paragraph_no}" if section else f"段落{paragraph_no}"
        chunks.append(CorpusChunk(
            text=block,
            locator=locator,
            locator_type="paragraph",
            sequence=paragraph_no,
        ))
    return ParsedCorpus(
        tuple(chunks),
        "paragraph" if chunks else "empty",
        bool(chunks),
        () if chunks else ("no_retrievable_paragraph",),
    )


def _is_section_heading(text: str) -> bool:
    return (
        len(text) <= 60
        and _SECTION.match(text) is not None
        and not text.endswith(("。", "；", ";"))
    )


__all__ = ["CorpusChunk", "ParsedCorpus", "split_recognized_fulltext"]
