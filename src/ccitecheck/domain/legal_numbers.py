"""中文法律序号的解析工具。"""

from __future__ import annotations


def chinese_number_to_int(value: str) -> int | None:
    """将中文或阿拉伯数字形式的法律序号转换为整数。"""
    text = value.strip()
    if text.isdigit():
        return int(text)
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "壹": 1,
        "贰": 2,
        "叁": 3,
        "肆": 4,
        "伍": 5,
        "陆": 6,
        "柒": 7,
        "捌": 8,
        "玖": 9,
    }
    units = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
    if not text or any(char not in digits and char not in units for char in text):
        return None
    if all(char in digits for char in text):
        return int("".join(str(digits[char]) for char in text))
    total = current = 0
    for char in text:
        if char in digits:
            current = digits[char]
        else:
            total += (current or 1) * units[char]
            current = 0
    return total + current


def int_to_chinese_number(value: int) -> str:
    """将 0 到 9999 的整数转换为常用中文数字。"""
    if not 0 <= value <= 9999:
        raise ValueError("value must be between 0 and 9999")
    if value == 0:
        return "零"
    digits = "零一二三四五六七八九"
    units = ((1000, "千"), (100, "百"), (10, "十"), (1, ""))
    result: list[str] = []
    pending_zero = False
    remainder = value
    for divisor, unit in units:
        digit, remainder = divmod(remainder, divisor)
        if digit:
            if pending_zero:
                result.append("零")
                pending_zero = False
            if not (divisor == 10 and digit == 1 and not result):
                result.append(digits[digit])
            result.append(unit)
        elif result and remainder:
            pending_zero = True
    return "".join(result)


__all__ = ["chinese_number_to_int", "int_to_chinese_number"]
