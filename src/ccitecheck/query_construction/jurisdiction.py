"""法规引用的法域识别。

国别线索通常在书名号之外（如「德国《著作权法》」），识别时结合
书名号前的紧邻修饰词与常见涉外法名别名表判定法域。只认紧邻前缀，
「大陆法系国家多规定」这类泛称不触发。

法域用 ISO-3166 国别代码（DE/FR/JP/GB/US/CA…），美国州、加拿大省
保留到地方层级（US-CA、CA-ON）；无法判定时返回 UNKNOWN，保留人工
核查入口。同名《著作权法》因法域代码不同不会跨国混用。
"""

from __future__ import annotations

import re

JURISDICTION_CN = "CN"
JURISDICTION_EU = "EU"
JURISDICTION_UNKNOWN = "UNKNOWN"

_EU_PREFIXES = ("欧盟", "欧洲联盟", "欧共体", "欧洲议会")
_CN_PREFIXES = ("中华人民共和国", "中国大陆", "中国", "我国")

# 国别中文前缀 → ISO-3166 alpha-2 代码。只认紧邻前缀。
_COUNTRY_PREFIXES: dict[str, str] = {
    "德国": "DE", "法国": "FR", "美国": "US", "日本": "JP", "英国": "GB",
    "韩国": "KR", "俄罗斯": "RU", "新加坡": "SG", "意大利": "IT",
    "西班牙": "ES", "荷兰": "NL", "瑞士": "CH", "瑞典": "SE",
    "挪威": "NO", "丹麦": "DK", "芬兰": "FI", "澳大利亚": "AU",
    "加拿大": "CA", "印度": "IN", "巴西": "BR", "南非": "ZA",
    "泰国": "TH", "越南": "VN", "马来西亚": "MY", "印度尼西亚": "ID",
    "阿根廷": "AR", "墨西哥": "MX",
}
_UN_PREFIXES = ("联合国",)

# 知名涉外法规的名称直接映射（书名号内即可判定，无需前缀）。
_TITLE_ALIASES: dict[str, str] = {
    "通用数据保护条例": JURISDICTION_EU,
    "一般数据保护条例": JURISDICTION_EU,
    "人工智能法案": JURISDICTION_EU,
    "人工智能法": JURISDICTION_EU,
    "EU AI Act": JURISDICTION_EU,
    "TAKE IT DOWN Act": "US",
    "数字市场法": JURISDICTION_EU,
    "数字服务法": JURISDICTION_EU,
    "数据法案": JURISDICTION_EU,
    "人工智能发展及建立信任基础基本法": "KR",
    # Code de la propriété intellectuelle —— 法国知识产权法典
    "知识产权法典": "FR",
}

_GDPR_PATTERN = re.compile(r"\bGDPR\b", re.IGNORECASE)

# 书名号前允许略过的收尾字符（顿号、空白），再往前取紧邻修饰词。
_PREFIX_WINDOW = 8

# 美国州名（含简称）→ 州邮编；仅匹配书名号前窗口内的州名。
_US_STATES: dict[str, str] = {
    "加利福尼亚州": "CA", "加州": "CA", "纽约州": "NY",
    "得克萨斯州": "TX", "德州": "TX", "佛罗里达州": "FL",
    "伊利诺伊州": "IL", "华盛顿州": "WA", "马萨诸塞州": "MA",
}
# 加拿大省份 → 省邮编。
_CA_PROVINCES: dict[str, str] = {
    "安大略省": "ON", "魁北克省": "QC", "不列颠哥伦比亚省": "BC",
    "阿尔伯塔省": "AB",
}

# 细分层级窗口：州/省名比国名更长，需更大窗口。
_SUBDIVISION_WINDOW = 16


def detect_jurisdiction(title: str, preceding_text: str = "") -> str:
    """判定一条《》引用的法域。

    Args:
        title: 书名号内的法规名（不含书名号）。
        preceding_text: 书名号之前的原文片段（任意长度，取尾部窗口判断）。

    Returns:
        ISO 国别代码（DE/FR/JP/GB/US/CA…）、带细分的代码（US-CA、CA-ON）、
        EU、CN，或 UNKNOWN。

    结构为 [国名][可选州/省名]《法名》：先扫州/省（蕴含国别），再查紧邻
    末尾的国名前缀，最后默认 CN。
    """
    return detect_jurisdiction_with_basis(title, preceding_text)[0]


def detect_jurisdiction_with_basis(
    title: str, preceding_text: str = ""
) -> tuple[str, str]:
    """返回法域及信号来源，供查询层区分明确线索和默认 CN。"""
    title_signal = _jurisdiction_in_title(title)
    adjacent_signal = _jurisdiction_before_title(preceding_text)
    if (
        title_signal is not None
        and adjacent_signal is not None
        and title_signal[0] != adjacent_signal[0]
    ):
        return JURISDICTION_UNKNOWN, "conflicting_explicit_signals"
    if adjacent_signal is not None:
        return adjacent_signal
    if title_signal is not None:
        return title_signal
    if re.search(r"[A-Za-z]", title):
        return JURISDICTION_UNKNOWN, "latin_unknown"
    return JURISDICTION_CN, "default_cn"


def _jurisdiction_in_title(title: str) -> tuple[str, str] | None:
    stripped = title.strip()
    alias = _TITLE_ALIASES.get(stripped)
    if alias is not None:
        return alias, "title_alias"
    if _GDPR_PATTERN.search(stripped):
        return JURISDICTION_EU, "title_token"
    for name, sub in _US_STATES.items():
        if stripped.startswith(name) or stripped.startswith(f"美国{name}"):
            return f"US-{sub}", "title_subdivision"
    for name, sub in _CA_PROVINCES.items():
        if stripped.startswith(name) or stripped.startswith(f"加拿大{name}"):
            return f"CA-{sub}", "title_subdivision"
    for prefix in _EU_PREFIXES:
        if stripped.startswith(prefix):
            return JURISDICTION_EU, "title_prefix"
    for prefix in _UN_PREFIXES:
        if stripped.startswith(prefix):
            return "UN", "title_prefix"
    for prefix in _CN_PREFIXES:
        if stripped.startswith(prefix):
            return JURISDICTION_CN, "title_prefix"
    for prefix, code in sorted(
        _COUNTRY_PREFIXES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if stripped.startswith(prefix):
            return code, "title_prefix"
    return None


def _jurisdiction_before_title(preceding_text: str) -> tuple[str, str] | None:
    window = preceding_text[-_PREFIX_WINDOW:].rstrip("、 \t　《")
    for prefix in _EU_PREFIXES:
        if window.endswith(prefix):
            return JURISDICTION_EU, "adjacent_prefix"
    for prefix in _UN_PREFIXES:
        if window.endswith(prefix):
            return "UN", "adjacent_prefix"
    for prefix in _CN_PREFIXES:
        if window.endswith(prefix):
            return JURISDICTION_CN, "adjacent_prefix"

    # 先扫州/省名（结构为 [国名][州/省名]《法名》，国名不在末尾）
    sub_window = preceding_text[-_SUBDIVISION_WINDOW:]
    for name, sub in _US_STATES.items():
        if name in sub_window:
            return f"US-{sub}", "adjacent_subdivision"
    for name, sub in _CA_PROVINCES.items():
        if name in sub_window:
            return f"CA-{sub}", "adjacent_subdivision"

    for prefix, code in _COUNTRY_PREFIXES.items():
        if window.endswith(prefix):
            return code, "adjacent_prefix"
    return None


def is_foreign(jurisdiction: str) -> bool:
    """是否为非中国、非欧盟的涉外法域（含 UNKNOWN 以外的具体国别）。"""
    return jurisdiction not in (
        JURISDICTION_CN,
        JURISDICTION_EU,
        JURISDICTION_UNKNOWN,
        "FOREIGN",
    )


__all__ = [
    "JURISDICTION_CN",
    "JURISDICTION_EU",
    "JURISDICTION_UNKNOWN",
    "detect_jurisdiction",
    "detect_jurisdiction_with_basis",
    "is_foreign",
]
