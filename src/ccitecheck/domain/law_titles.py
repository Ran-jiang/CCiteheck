"""中国法规名称中“中华人民共和国”前缀的纯形态规则。"""

from __future__ import annotations


CN_TITLE_PREFIX = "中华人民共和国"


def canonical_cn_title_shape(title: str) -> str:
    """返回带国家全称前缀的名称形态；不判断该法规是否真实存在。"""
    value = title.strip()
    return value if not value or value.startswith(CN_TITLE_PREFIX) else f"{CN_TITLE_PREFIX}{value}"


def cn_title_shape_variants(title: str) -> tuple[str, ...]:
    """按长名、短名顺序返回两个确定性形态。"""
    canonical = canonical_cn_title_shape(title)
    short = canonical.removeprefix(CN_TITLE_PREFIX)
    return tuple(dict.fromkeys(value for value in (canonical, short) if value))


def cn_title_shape_key(title: str) -> str:
    """仅忽略空白和国家全称前缀，供识别结果去重。"""
    return "".join(title.split()).removeprefix(CN_TITLE_PREFIX)


__all__ = [
    "CN_TITLE_PREFIX",
    "canonical_cn_title_shape",
    "cn_title_shape_key",
    "cn_title_shape_variants",
]
