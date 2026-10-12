"""裁判说理文本的清洗、切句与编号。

北大法宝案例记录的 holding 字段实为判决书"本院认为"说理部分的节选，
存在硬换行、匿名化符号与尾部截断等数据质量问题。本模块把说理文本
整理成可编号引用的句子列表，供观点核查按句号定位、按原文呈现。
"""

from __future__ import annotations

import re

_HARD_WRAP = re.compile(r"\s*\n\s*")
_WHITESPACE = re.compile(r"[ \t　]+")
_SENTENCE_END = re.compile(r"([。；！？])")
_TERMINAL_PUNCTUATION = ("。", "；", "！", "？", "：", "”", "）", ")")


def clean_reasoning_text(text: str) -> str:
    """去除供应商文本中的硬换行和多余空白，不改动任何有效字符。"""
    cleaned = _HARD_WRAP.sub("", text.strip())
    return _WHITESPACE.sub("", cleaned)


def split_reasoning_sentences(text: str) -> list[str]:
    """按句号、分号、叹号、问号切句，标点保留在句尾。"""
    cleaned = clean_reasoning_text(text)
    if not cleaned:
        return []
    parts = _SENTENCE_END.split(cleaned)
    sentences = []
    for index in range(0, len(parts), 2):
        body = parts[index].strip()
        punctuation = parts[index + 1] if index + 1 < len(parts) else ""
        if body:
            sentences.append(body + punctuation)
    return sentences


def reasoning_is_truncated(sentences: list[str]) -> bool:
    """末句缺少终止标点视为供应商文本疑似截断。"""
    if not sentences:
        return False
    return not sentences[-1].endswith(_TERMINAL_PUNCTUATION)


def build_excerpt(sentences: list[str], hit_ids: list[int]) -> str:
    """按命中句号取原文拼接展示引文；相邻句合并，跳段以省略号衔接。"""
    valid = sorted({hit for hit in hit_ids if 1 <= hit <= len(sentences)})
    if not valid:
        return ""
    groups: list[list[int]] = [[valid[0]]]
    for hit in valid[1:]:
        if hit == groups[-1][-1] + 1:
            groups[-1].append(hit)
        else:
            groups.append([hit])
    body = "……".join(
        "".join(sentences[hit - 1] for hit in group) for group in groups
    )
    return f"……{body}……"


__all__ = [
    "build_excerpt",
    "clean_reasoning_text",
    "reasoning_is_truncated",
    "split_reasoning_sentences",
]
