"""
CCiteheck 法律文本分句器。

纯函数分句器，使用单遍扫描 + 嵌套状态计数器。
保证分句无损：join(sentences) == 原文。

分句规则：
  - 强切分符：。！？
  - 分号不切分，避免拆散前提、义务、但书和例外等完整法律表述
  - 半角句点 . 不切分
  - 逗号、顿号不切分
  - 闭合符号归属前一句
  - 书名号/引号/括号 内部不切分（除非强切分符在末尾且紧跟关闭符号）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SentenceSpan:
    """句子跨度信息"""
    text: str
    char_start: int  # 在原始文本中的起始偏移（左闭）
    char_end: int    # 在原始文本中的结束偏移（右开）


# 强切分符（全角标点，在这些字符位置可以切句）
STRONG_BREAKS = {"。", "！", "？"}

# 闭合符号：出现在切分符之后时，归属前一句
CLOSING_CHARS = {"”", "’", "″", "\"", "」", "』", "》", "）", ")", "]", "〕", "］"}

# 闭合符号对应的开放符号
OPEN_TO_CLOSE = {
    "《": "》",  # 《》
    "（": "）",  # （）
    "(": ")",
    "“": "”",  # ""
    "\"": "\"",
    "【": "】",  # 【】
    "[": "]",
    "『": "』",  # 『』
    "「": "」",  # 「」
}

# 开放符号集合
OPENING_CHARS = set(OPEN_TO_CLOSE.keys())

# 将所有开放和闭合符号打入集合，供快速查找
ALL_SPECIAL = STRONG_BREAKS | CLOSING_CHARS | OPENING_CHARS | set(OPEN_TO_CLOSE.values())


def split_sentences(text: str) -> list[SentenceSpan]:
    """
    将文本按法律文本规则切分为句子。

    使用单遍扫描，维护嵌套状态。

    Args:
        text: 待切分的文本

    Returns:
        SentenceSpan 列表，保证 join 后等于原文
    """
    if not text:
        return []
    return _split_sentences_state_machine(text)


def _split_sentences_state_machine(text: str) -> list[SentenceSpan]:
    """
    使用状态机实现的分句器。

    核心思路：
    1. 扫描找到所有强切分符位置
    2. 特殊规则：强切分符位于包裹结构末尾，后面紧跟关闭符号时，允许切分
    3. 强切分符后面的闭合符号归属前一句
    4. 在闭合符号之后（或强切分符之后无闭合符号时）切分
    5. 嵌套结构内部不切分
    """
    if not text:
        return []

    n = len(text)
    # 记录每个位置之后是否可以切分（True 表示此字符是当前句的最后一个字符）
    is_break_after: list[bool] = [False] * n

    # 嵌套状态
    angle_depth = 0
    paren_depth = 0
    quote_depth = 0
    bracket_depth = 0

    # 第一遍：标记所有强切分符位置
    for i, ch in enumerate(text):
        # 更新嵌套状态
        if ch == "《":  # 《
            angle_depth += 1
        elif ch == "》":  # 》
            angle_depth = max(0, angle_depth - 1)
        elif ch == "（":  # （
            paren_depth += 1
        elif ch == "）":  # ）
            paren_depth = max(0, paren_depth - 1)
        elif ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth = max(0, paren_depth - 1)
        elif ch == "“":  # 左中文引号
            quote_depth += 1
        elif ch == "”":  # 右中文引号
            quote_depth = max(0, quote_depth - 1)
        elif ch == "\"":
            # 英文引号简单切换
            quote_depth = 1 - quote_depth
        elif ch == "【":  # 【
            bracket_depth += 1
        elif ch == "】":  # 】
            bracket_depth = max(0, bracket_depth - 1)
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]":
            bracket_depth = max(0, bracket_depth - 1)

        # 判断是否为强切分符
        if ch in STRONG_BREAKS:
            total_depth = angle_depth + paren_depth + quote_depth + bracket_depth
            if total_depth == 0:
                # 所有嵌套已闭合 -> 正常切分
                is_break_after[i] = True
            elif total_depth == 1:
                # 位于单层嵌套内，检查后面是否紧跟闭合符号
                # 如果是，允许在此切分（闭合符号会归属到前一句）
                j = i + 1
                # 跳过空白字符
                while j < n and text[j] in (" ", "　", "\t"):
                    j += 1
                if j < n and text[j] in CLOSING_CHARS:
                    is_break_after[i] = True

    # 第二遍：处理闭合符号归属
    # 如果切分符后面紧跟闭合符号，将闭合符号归属到前一句，
    # 切分点移到闭合符号之后
    adjusted_breaks: list[bool] = [False] * n
    i = 0
    while i < n:
        if is_break_after[i]:
            # 找到真正的切分位置：跳过后续闭合符号
            cut_pos = i
            j = i + 1
            while j < n and text[j] in CLOSING_CHARS:
                cut_pos = j
                j += 1
            adjusted_breaks[cut_pos] = True
            i = cut_pos + 1  # 从切分位置之后继续
        else:
            i += 1

    # 第三遍：根据调整后的切分点生成句子
    sentences: list[SentenceSpan] = []
    sent_start = 0
    for i, is_break in enumerate(adjusted_breaks):
        if is_break:
            char_start = sent_start
            char_end = i + 1  # 右开区间，包含切分符
            sent_text = text[char_start:char_end]
            sentences.append(SentenceSpan(
                text=sent_text,
                char_start=char_start,
                char_end=char_end,
            ))
            sent_start = i + 1

    # 处理最后一句（如果还有剩余文本）
    if sent_start < n:
        sentences.append(SentenceSpan(
            text=text[sent_start:n],
            char_start=sent_start,
            char_end=n,
        ))

    return sentences
