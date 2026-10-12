"""
CCiteheck 标题识别器。

采用双轨机制：
  1. 样式优先：通过段落样式名（Heading N / 标题 N）识别
  2. 模式 fallback：通过正则模式匹配识别伪标题
     - 中文章节标题：第X编/章/节
     - 数字编号标题：1、1.1、1.1.1 等

重要规则：
  - "第X条" 不是标题
  - 宁可漏判，不要误判
  - 层级动态对齐
"""

from __future__ import annotations

import re
from typing import Optional

from ..domain.document import HeadingSource
from .utils import is_article_start


# ---- 样式识别 ----
# DOCX 中常见的标题样式名
HEADING_STYLE_PATTERNS = [
    r"^Heading\s*(\d+)$",       # "Heading 1", "Heading 2"
    r"^标题\s*(\d+)$",           # "标题 1", "标题 2"
    r"^heading\s*(\d+)$",        # "heading 1", "heading 2"
]


def detect_heading_level_from_style(style_name: Optional[str]) -> Optional[int]:
    """
    根据段落样式名检测标题层级。

    Args:
        style_name: DOCX 段落样式名称（可能为 None）

    Returns:
        标题层级（1-based），如果不是标题样式则返回 None
    """
    if not style_name:
        return None
    for pattern in HEADING_STYLE_PATTERNS:
        m = re.match(pattern, style_name)
        if m:
            return int(m.group(1))
    return None


# ---- 标题模式回退 ----
# 中文编章节标题
# 格式：第 + 数字/汉字 + 编/章/节
CHAPTER_PATTERN = re.compile(
    r"^第([一二三四五六七八九十百千零〇\d]+)([编章节])"
)

# 数字编号标题：1 xxx、1.概述、1.1 xxx、1.1.xxx
NUMBERED_PATTERN = re.compile(
    r"^(\d+(?:\.\d+)*)(?:[.、]\s*|\s+)(?=\S)"
)

CHINESE_NUMBERED_PATTERN = re.compile(
    r"^[一二三四五六七八九十百千]+、\s*(?=\S)"
)

EXPLICIT_HEADING_TEXTS = {"概述", "前言", "引言", "总结", "结语", "附录"}
HEADING_END_PUNCTUATION = "。！？；：，,;:!?"

# 伪标题最大长度（字符数）
MAX_PSEUDO_HEADING_LENGTH = 40


def detect_chapter_type(text: str) -> Optional[tuple[str, int]]:
    """
    检测中文章节标题，返回（类型字符, 原始层级）。

    类型字符：'编'、'章'、'节'
    原始层级：编=1, 章=2, 节=3

    Args:
        text: 段落文本

    Returns:
        (类型字符, 原始层级) 或 None
    """
    m = CHAPTER_PATTERN.match(text)
    if m:
        ch_type = m.group(2)
        raw_level = {"编": 1, "章": 2, "节": 3}[ch_type]
        return (ch_type, raw_level)
    return None


def detect_numbered_heading(text: str) -> Optional[int]:
    """
    检测数字编号标题，返回层级。

    数字编号规则：按点号深度推断层级
      1 xxx    → 层级 1
      1.1 xxx  → 层级 2
      1.1.1 xxx → 层级 3

    Args:
        text: 段落文本

    Returns:
        层级或 None
    """
    if text.endswith(tuple(HEADING_END_PUNCTUATION)):
        return None
    m = NUMBERED_PATTERN.match(text)
    if m:
        number_part = m.group(1)
        # 点号数量 + 1 = 层级
        level = number_part.count(".") + 1
        return level
    if CHINESE_NUMBERED_PATTERN.match(text):
        return 1
    return None


def normalize_heading_levels(
    chapter_types: set[str],
    raw_level: int,
) -> int:
    """
    动态对齐标题层级。

    根据文档中出现过的编/章/节类型，将原始层级偏移为从1开始的连续层级。

    例如文档中只有"章"和"节"（没有"编"），则：
      - "章" 的原始层级 2 → 调整为 1
      - "节" 的原始层级 3 → 调整为 2

    Args:
        chapter_types: 文档中出现过的章节类型集合 {"编", "章", "节"}
        raw_level: 原始层级（1=编, 2=章, 3=节）

    Returns:
        调整后的层级（1-based，连续递增）
    """
    if not chapter_types:
        return raw_level
    # 按原始层级排序去重
    sorted_types = sorted(chapter_types, key=lambda t: {"编": 1, "章": 2, "节": 3}[t])
    # 创建映射：原始层级 → 调整后层级
    level_map = {}
    for new_level, ch_type in enumerate(sorted_types, start=1):
        raw = {"编": 1, "章": 2, "节": 3}[ch_type]
        level_map[raw] = new_level
    return level_map.get(raw_level, raw_level)


def is_pseudo_heading(text: str) -> Optional[tuple[int, HeadingSource]]:
    """
    检测伪标题（模式 fallback）。

    规则：
      1. 段落长度 ≤ 40 字
      2. 匹配中文章节标题 或 数字编号标题
      3. 不是"第X条"格式
      4. 非空文本

    Args:
        text: 段落文本

    Returns:
        (层级, 识别来源) 或 None
    """
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) > MAX_PSEUDO_HEADING_LENGTH:
        return None
    # "第X条" 不是 heading
    if is_article_start(text):
        return None

    if text in EXPLICIT_HEADING_TEXTS:
        return (1, HeadingSource.PATTERN)

    # 尝试中文章节标题
    chapter_result = detect_chapter_type(text)
    if chapter_result:
        ch_type, raw_level = chapter_result
        # 此时还不知道文档中有哪些章节类型，返回原始层级
        # 调用方需要在扫描全文档后调用 normalize_heading_levels
        return (raw_level, HeadingSource.PATTERN)

    # 尝试数字编号标题
    num_level = detect_numbered_heading(text)
    if num_level is not None:
        return (num_level, HeadingSource.PATTERN)

    return None


def detect_heading(
    text: str,
    style_name: Optional[str],
    chapter_types_in_doc: Optional[set[str]] = None,
) -> Optional[tuple[int, HeadingSource]]:
    """
    综合标题检测。

    双轨机制：
      1. 样式优先
      2. 模式 fallback

    Args:
        text: 段落文本
        style_name: 段落样式名
        chapter_types_in_doc: 文档中出现过的章节类型（用于层级对齐）

    Returns:
        (层级, 识别来源) 或 None
    """
    if chapter_types_in_doc is None:
        chapter_types_in_doc = set()

    # 样式优先
    style_level = detect_heading_level_from_style(style_name)
    if style_level is not None:
        return (style_level, HeadingSource.STYLE)

    # 样式未命中时使用标题文本模式回退。
    result = is_pseudo_heading(text)
    if result is not None:
        raw_level, source = result
        if source == HeadingSource.PATTERN:
            # 检查是否为中文章节标题
            ch_result = detect_chapter_type(text)
            if ch_result:
                ch_type, _ = ch_result
                chapter_types_in_doc.add(ch_type)
                adjusted = normalize_heading_levels(chapter_types_in_doc, raw_level)
                return (adjusted, HeadingSource.PATTERN)
        return (raw_level, source)

    return None


def scan_chapter_types(texts: list[str]) -> set[str]:
    """
    扫描文档中所有段落，收集出现过的章节类型。

    用于后续动态层级对齐。

    Args:
        texts: 所有段落文本列表

    Returns:
        出现过的章节类型集合
    """
    types: set[str] = set()
    for text in texts:
        m = CHAPTER_PATTERN.match(text.strip())
        if m:
            types.add(m.group(2))
    return types
