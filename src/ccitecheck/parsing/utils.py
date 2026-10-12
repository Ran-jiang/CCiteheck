"""
CCiteheck 解析工具函数。

包含：
  - 空白字符判断
  - 文本空判断
  - id 生成器
  - token 估算
"""

import re
import string as string_module

# ---- 空白字符集 ----
# 空白包括半角空格、全角空格、制表符、换行和回车。
WHITESPACE_CHARS = " 　\t\n\r"


def is_empty_text(text: str) -> bool:
    """
    判断文本去除指定空白字符后是否为空。

    Args:
        text: 待判断文本

    Returns:
        True 如果去除空白后为空字符串
    """
    if not text:
        return True
    return text.strip(WHITESPACE_CHARS) == ""


def normalize_whitespace(text: str) -> str:
    """
    归一化文本中的空白字符。

    软换行和制表符替换为一个半角空格。
    去除非空白字符之间的多余空白。

    Args:
        text: DOCX 段落原始文本（已包含从 w:br 和 tab 替换来的字符）

    Returns:
        归一化后的文本
    """
    if not text:
        return ""
    # 将软换行和制表符统一替换为半角空格
    # 注意：这一步在 docx_parser 中已在拼接时完成，
    # 此处作为后备处理
    text = text.replace("\n", " ").replace("\t", " ").replace("\r", " ")
    # 合并多个连续空格为单个空格
    text = re.sub(r" +", " ", text)
    # 去除首尾空白
    text = text.strip()
    return text


# ---- ID 生成器 ----
# 使用闭包维护计数器，确保 ID 在单次解析中连续递增


def make_id_counter(prefix: str, width: int = 5):
    """
    创建 ID 计数器。

    Args:
        prefix: ID 前缀，如 "line"、"b_"、"c_"、"lg_"
        width: 数字部分的宽度，默认5位零填充

    Returns:
        无参函数，每次调用返回下一个 ID 字符串
    """
    counter = [0]  # 使用列表以实现闭包内可变

    def next_id() -> str:
        counter[0] += 1
        return f"{prefix}{counter[0]:0{width}d}"

    return next_id


# ---- Token 估算 ----
# 粗估算：中文字符按 1 token，ASCII 字符按 0.5 token，其他字符按 1 token。
# 这是粗略上限估算，不是 tokenizer 精确结果。


def estimate_tokens(text: str) -> int:
    """
    估算文本的 token 数量。

    规则：
      - 中文字符（CJK统一表意文字）：1 token
      - ASCII 可打印字符：0.5 token
      - 其他字符：1 token

    Args:
        text: 输入文本

    Returns:
        估算的 token 数量（取整）
    """
    count = 0.0
    for ch in text:
        if "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿":
            # CJK 统一表意文字 或 CJK 扩展A
            count += 1.0
        elif ch in string_module.printable and ord(ch) < 128:
            # ASCII 可打印字符
            count += 0.5
        else:
            count += 1.0
    return round(count)


# ---- 编号解析工具 ----


ARTICLE_START_PATTERN = re.compile(
    r"^第[一二三四五六七八九十百千零〇\d]+条"
)


def is_article_start(text: str) -> bool:
    """
    检测段落是否为"第X条"起始。

    匹配模式：^第[一二三四五六七八九十百千零〇0-9]+条

    Args:
        text: 段落文本

    Returns:
        True 如果文本匹配第X条格式
    """
    return bool(ARTICLE_START_PATTERN.match(text))


def detect_chinese_list_item(text: str) -> bool:
    """
    检测段落是否为中文列举项。
    匹配：
      - ^（[一二三四五六七八九十]+）
      - ^[一二三四五六七八九十]+、

    Args:
        text: 段落文本

    Returns:
        True 如果匹配列举项格式
    """
    if not text:
        return False
    pattern1 = r"^（[一二三四五六七八九十]+）"  # 全角括号包围
    pattern2 = r"^[一二三四五六七八九十]+、"  # 中文数字 + 顿号
    return bool(re.match(pattern1, text) or re.match(pattern2, text))
