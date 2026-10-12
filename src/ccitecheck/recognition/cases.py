"""
CCiteheck 案号与案例线索识别。

负责：
  1. 识别有案号的案例引用（with_case_number）
  2. 识别无案号的案例线索（without_case_number）——白名单模式，宁漏勿错
  3. 从上下文提取法院名称
  4. 检测观点触发词（用于 case_holding_paraphrase 判定）

设计决策：
  - 有案号：精确匹配 [（(〔] + 年份 + 法院简称 + 案件类型字 + 号
  - 无案号：仅识别白名单中的可检索线索（指导案例、公报案例、典型案例、命名案）
  - 显式排除指代词：本案、该案、此案、上述案件、前案、原案
  - 没有明确 case_ref 时绝不因"法院认为""本院认为"而抽取 case_holding_paraphrase
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Optional

from ..domain.citation import CaseRef


# ============================================================
# 案号正则
# ============================================================

# 案号模式：
# 括号（全角/半角/方括号）+ 年份（1或2开头的4位数字）+ 法院简称 + 案件类型字 + 数字 + 号
# 示例：（2021）最高法民申1234号 / (2021)京73民终123号 / 〔2019〕粤民再56号
# 注意：括号形态有3种——全角（）、半角()、方头括号〔〕
CASE_NUMBER_PATTERN = re.compile(
    r"[（(〔]"
    r"([12]\d{3})"           # 年份（1xxx 或 2xxx）
    r"[）)〕]"
    r"[^\s，。；：、（）()〔〕]{0,12}?"  # 有界法院代码（旧案号可省略）
    r"[民刑行知赔执破清海商]"             # 案件类型字
    r"[^\s，。；：、（）()〔〕]{0,6}?"   # 初/终/再/申等附加分类
    r"(?:字第)?\d+"                         # 新旧案号数字格式
    r"号"
)

# 案号后紧跟的文书类型，如"（2018）最高法民再396号民事判决书"
CASE_DOCUMENT_TYPE_PATTERN = re.compile(
    r"^(?:之[一二三四五六七八九十])?(?:民事|刑事|行政|执行)?"
    r"(判决书|裁定书|调解书|决定书|支付令)"
)

# 无案号但具有完整裁判文书标题结构的案例。要求同时出现案由尾词和正式
# 文书类型，避免把“提交判决书”等普通叙述识别为案例名称。
JUDGMENT_DOCUMENT_TITLE_PATTERN = re.compile(
    r"[^。！？；，\n\t]{2,180}?"
    r"(?:纠纷|责任纠纷|争议)"
    r"(?:民事|刑事|行政)?(?:一审|二审|再审)?(?:民事|刑事|行政)?"
    r"(?:判决书|裁定书)"
)

# 无案号线索 — 白名单模式

# 指导案例：指导案例第X号 或 指导性案例第X号
GUIDING_CASE_PATTERN = re.compile(
    r"指导(?:性)?案例(?:第)?[一二三四五六七八九十百\d]+号"
)

# 公报案例 / 典型案例
GAZETTE_TYPICAL_PATTERN = re.compile(
    r"(公报案例|典型案例)"
)

# 命名案模式：X诉Y……案 / X诉Y……纠纷案
# "A诉B……案"结构，A和B至少各2个字符（人名/公司名）
# 约束：
#   1. 当事人名限长 2-10 字（原 \S{2,}? 无上限，会把
#      "投诉情况……做出预案"这类整句误判为案名）
#   2. "诉"前不能是被/投/申/上/起/控（排除被诉、投诉、申诉、上诉、起诉、控诉）
#   3. "诉"后不能是讼/称/求/请（排除诉讼、诉称、诉求、诉请）
#   4. "诉"到"案"之间最多12字，且不得跨越句读（。！？；，、）
#   5. "案"前不能是预/方/草/议/答/备/提/立/档（排除预案、方案、草案等）
NAMED_CASE_PATTERN = re.compile(
    r"[^。！？；，\n]{2,60}?(?<![被投申上起控])诉(?![讼称求请]|行为)"
    r"[^。！？；，\n]{1,60}?(?:纠纷)?(?<![预方草议答备提立档])案[）)]?"
)

# 命名案例中连接原告与被告的“诉”。候选清理前后使用同一判定，避免
# 《民诉解释》中的“诉”在前缀被截掉后，残余正文仍被当作案名。
_NAMED_CASE_SEPARATOR = re.compile(
    r"(?<![被投申上起控])诉(?![讼称求请]|行为)"
)


# 外国判例引用 — 英文案名（Roe v. Wade）与美式判例汇编引注（347 U.S. 483）。
    # 这类案例不在中文案例库核查范围内，识别后标记 jurisdiction=UNKNOWN，
# 由判定层显式告知"超出核查边界"，避免静默丢弃。
FOREIGN_CASE_NAME_PATTERN = re.compile(
    r"\b[A-Z][\w.&'’\-]*(?:\s+[A-Z][\w.&'’\-]*){0,4}"
    r"\s+vs?\.\s+"
    r"[A-Z][\w.&'’\-]*(?:\s+[A-Z][\w.&'’\-]*){0,4}"
)
FOREIGN_REPORTER_PATTERN = re.compile(
    r"\b\d{1,4}\s+(?:U\.\s?S\.|S\.\s?Ct\.|F\.(?:\s?[23]d)?|F\.\s?Supp\.?)\s+\d{1,4}\b"
)

# 指代词（指向当前文书内部，不可外部检索）— 显式排除
PRONOUN_PATTERNS = [
    re.compile(r"本案"),
    re.compile(r"该案"),
    re.compile(r"此案"),
    re.compile(r"上述案件"),
    re.compile(r"前案"),
    re.compile(r"原案"),
]

# 观点触发词（用于 case_holding_paraphrase 判定）
HOLDING_TRIGGER_PATTERN = re.compile(
    r"(裁判要旨|裁判规则|裁判认为|法院认为|认为|指出|明确|表明|确立|载明|记载|判称)"
)


# ============================================================
# 法院名提取
# ============================================================

# 常见法院名关键词
COURT_KEYWORDS = [
    "最高人民法院", "最高人民检察院",
    "高级人民法院", "中级人民法院", "基层人民法院",
    "知识产权法院", "互联网法院", "金融法院", "海事法院",
    "军事法院", "铁路运输法院", "铁路运输中级法院",
]

_FORMAL_COURT_PATTERN = re.compile(
    r"(?:(?:[一-鿿]{2,12})(?:省|市|自治区|特别行政区|自治州|地区|盟))?"
    r"(?:最高人民法院|高级人民法院|中级人民法院|基层人民法院|"
    r"知识产权法院|互联网法院|金融法院|海事法院|军事法院|"
    r"铁路运输中级法院|铁路运输法院)"
)
_COURT_ALIAS_PATTERN = re.compile(
    r"最高院|[一-鿿]{2,8}?(?:高院|中院)"
)
_COURT_PREFIX_WORDS = ("不服", "案中", "由", "经", "在", "向", "号")
_MUNICIPALITIES = {"北京", "上海", "天津", "重庆"}


def _normalize_court_alias(value: str) -> str:
    if value == "最高院":
        return "最高人民法院"
    level = "高级人民法院" if value.endswith("高院") else "中级人民法院"
    place = value[:-2]
    for prefix in _COURT_PREFIX_WORDS:
        if prefix in place:
            place = place.rsplit(prefix, 1)[-1]
    if place.endswith(("省", "市", "自治区", "特别行政区", "自治州", "地区", "盟")):
        locality = place
    elif place in _MUNICIPALITIES:
        locality = f"{place}市"
    elif level == "高级人民法院":
        locality = f"{place}省"
    else:
        locality = f"{place}市"
    return locality + level


def _clean_formal_court(value: str) -> str:
    for prefix in _COURT_PREFIX_WORDS:
        if prefix in value:
            value = value.rsplit(prefix, 1)[-1]
    return value


def _court_mentions(text: str) -> list[tuple[int, int, str]]:
    mentions = [
        (match.start(), match.end(), _clean_formal_court(match.group(0)))
        for match in _FORMAL_COURT_PATTERN.finditer(text)
    ]
    occupied = [(start, end) for start, end, _ in mentions]
    for match in _COURT_ALIAS_PATTERN.finditer(text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        mentions.append(
            (match.start(), match.end(), _normalize_court_alias(match.group(0)))
        )
    return sorted(mentions)


def extract_court_from_context(text: str) -> Optional[str]:
    """
    从文本中提取法院名称。

    搜索常见法院名关键词，返回找到的名称。
    如果有多个法院名，返回距离案号最近的一个。

    Args:
        text: 完整文本

    Returns:
        法院名称或 None
    """
    mentions = _court_mentions(text)
    return mentions[-1][2] if mentions else None


def _court_near_case_number(text: str, start: int, end: int) -> Optional[str]:
    """只在案号所在句内关联法院，兼容“案号……由某法院审理”。"""
    left = max(text.rfind(mark, 0, start) for mark in "。！？；\n") + 1
    right_candidates = [text.find(mark, end) for mark in "。！？；\n"]
    right = min((pos for pos in right_candidates if pos >= 0), default=len(text))
    sentence = text[left:right]
    mentions = _court_mentions(sentence)
    if not mentions:
        return None
    local_start = start - left
    local_end = end - left
    return min(
        mentions,
        key=lambda item: min(abs(item[1] - local_start), abs(item[0] - local_end)),
    )[2]


# ============================================================
# 案例引用提取
# ============================================================

def _has_self_reference(text: str) -> bool:
    """
    检查文本是否含指代词（指向当前文书本身）。

    这些词指向当前文书内部，不可外部检索，
    因此不构成案例引用。

    Args:
        text: 待检查文本

    Returns:
        True 如果含指代词
    """
    for pattern in PRONOUN_PATTERNS:
        if pattern.search(text):
            return True
    return False


def extract_case_refs(text: str) -> list[CaseRef]:
    """
    从文本中提取所有案例引用。

    有案号时填写 case_number；无案号时填写可检索的 case_name。
    含指代词 → 不提取

    Args:
        text: 待分析文本（通常是一个 anchor 的文本）

    Returns:
        CaseRef 列表
    """
    named_cases = list(_extract_valid_named_case_candidates(text))

    # 纯“本案/该案”等指代不可检索；同句另有经过结构裁定的外部案例线索时仍保留。
    has_explicit_reference = any(
        pattern.search(text)
        for pattern in (
            CASE_NUMBER_PATTERN,
            GUIDING_CASE_PATTERN,
            GAZETTE_TYPICAL_PATTERN,
        )
    ) or bool(named_cases)
    if _has_self_reference(text) and not has_explicit_reference:
        return []

    case_refs: list[CaseRef] = []

    # 1. 有案号
    case_number_matches = CASE_NUMBER_PATTERN.finditer(text)
    for m in case_number_matches:
        full_match = m.group(0)
        court = _court_near_case_number(text, m.start(), m.end())
        doc_type_match = CASE_DOCUMENT_TYPE_PATTERN.match(text[m.end():m.end() + 10])
        case_refs.append(CaseRef(
            case_number=full_match,
            case_name=None,
            court=court,
            document_type=doc_type_match.group(1) if doc_type_match else None,
        ))

    # 2. 指导案例
    for m in GUIDING_CASE_PATTERN.finditer(text):
        case_refs.append(CaseRef(
            case_number=None,
            case_name=m.group(0),
            court=None,
        ))

    # 3. 公报案例 / 典型案例
    for m in GAZETTE_TYPICAL_PATTERN.finditer(text):
        if named_cases:
            continue
        prefix = text[max(0, m.start() - 8):m.start()]
        # “附录二：典型案例”只是章节标题，不是可外部检索的具体案例。
        if re.search(r"附录[一二三四五六七八九十\d]*[：:]?\s*$", prefix):
            continue
        case_refs.append(CaseRef(
            case_number=None,
            case_name=m.group(0),
            court=None,
        ))

    # 4. 命名案（X诉Y……案）
    for case_name in named_cases:
        numbered = [ref for ref in case_refs if ref.case_number]
        guiding = [
            ref
            for ref in case_refs
            if ref.case_name and GUIDING_CASE_PATTERN.fullmatch(ref.case_name)
        ]
        # “命名案（案号）”及“指导案例X号：命名案”都是同一案例的两种线索，
        # 合并到一个 CaseRef，避免重复核验与重复结果卡片。
        if len(numbered) == 1:
            numbered[0].case_name = case_name
            continue
        if len(guiding) == 1:
            guiding[0].case_name = f"{guiding[0].case_name}：{case_name}"
            continue
        # 避免与已添加的重复
        if not any(c.case_name == case_name for c in case_refs):
            case_refs.append(CaseRef(
                case_number=None,
                case_name=case_name,
                court=None,
            ))

    # 5. 无案号完整裁判文书标题
    for match in JUDGMENT_DOCUMENT_TITLE_PATTERN.finditer(text):
        case_name = match.group(0).strip()
        if any(ref.case_name == case_name for ref in case_refs):
            continue
        document_type = "判决书" if case_name.endswith("判决书") else "裁定书"
        case_refs.append(CaseRef(
            case_number=None,
            case_name=case_name,
            court=None,
            document_type=document_type,
        ))

    # 6. 外国判例（英文案名 / 美式判例引注）→ 标记超出核查边界
    for pattern in (FOREIGN_CASE_NAME_PATTERN, FOREIGN_REPORTER_PATTERN):
        for m in pattern.finditer(text):
            clue = m.group(0).strip()
            if any(c.case_name == clue for c in case_refs):
                continue
            case_refs.append(CaseRef(
                case_number=None,
                case_name=clue,
                court=None,
                jurisdiction="UNKNOWN",
            ))

    return case_refs


def _extract_valid_named_case_candidates(text: str) -> Iterator[str]:
    """召回并裁定可外部检索的“X诉Y案”名称。"""
    for match in NAMED_CASE_PATTERN.finditer(text):
        # 正则可在“案件”的“案”处提前结束；这种普通名词不是命名案例结尾。
        if match.end() < len(text) and text[match.end()] == "件":
            continue
        name = _normalize_named_case_candidate(match.group(0))
        if _is_valid_named_case(name):
            yield name


def _normalize_named_case_candidate(raw_text: str) -> str:
    """清理候选边界；裁定由 ``_is_valid_named_case`` 单独负责。"""
    name = raw_text.strip()
    if "：" in name or ":" in name:
        name = re.split(r"[：:]", name)[-1].strip()
    name = re.sub(r"^(?:可参见|参见|例如|譬如|如|案例)\s*", "", name)
    name = re.sub(r"^(?:中?包括|其中包括|包括)\s*", "", name)
    name = name.lstrip("0123456789一二三四五六七八九十百号、：: ")
    return name.strip("“”‘’\"' ")


def _is_valid_named_case(name: str) -> bool:
    """验证清理后的名称仍具有完整的当事人“X诉Y案”结构。"""
    if not name or len(name) > 90 or not name.endswith(("案", "案）", "案)")):
        return False
    core = name.removesuffix("）").removesuffix(")")
    if core.endswith(("本案", "该案", "此案", "前案", "原案")):
        return False
    separator = _NAMED_CASE_SEPARATOR.search(core)
    if separator is None:
        return False
    left = core[:separator.start()].strip("“”‘’\"'（）() ")
    right = core[separator.end():-1].strip("“”‘’\"'（）() ")
    return bool(left and right)


def find_holding_trigger_position(
    text: str,
    case_refs: list[CaseRef],
) -> Optional[int]:
    """
    检测是否存在观点触发词，返回观点文本的起始位置。

    前提：必须有明确的 case_ref。
    没有明确 case_ref 时，即使出现"法院认为""本院认为"也不触发。

    规则：查找最后一个 case_ref 之后是否出现"认为/指出/
    明确/裁判认为/法院认为/裁判要旨/裁判规则"等观点触发词。

    Args:
        text: 完整文本
        case_refs: 已提取的案例引用列表

    Returns:
        观点文本起始位置，如果没有触发则返回 None
    """
    if not case_refs:
        return None

    # 找到最后一个 case_ref 在文本中的位置
    last_ref_pos = -1
    for cr in case_refs:
        if cr.case_number:
            pos = text.find(cr.case_number)
        elif cr.case_name:
            pos = text.find(cr.case_name)
        else:
            continue
        if pos > last_ref_pos:
            last_ref_pos = pos

    if last_ref_pos < 0:
        return None

    # 在 case_ref 之后查找观点触发词
    after_ref = text[last_ref_pos:]
    # 先跳过 case_ref 本身
    # 找到第一个非 case_ref 内容的起始
    # 简化处理：在整段后面搜索触发词
    trigger_match = HOLDING_TRIGGER_PATTERN.search(after_ref)
    if not trigger_match:
        return None

    trigger_end = trigger_match.end()
    remaining = after_ref[trigger_end:]

    # 去除触发词后紧跟的逗号、冒号
    remaining = re.sub(r"^[，,：:\s]+", "", remaining)

    if not remaining or not remaining.strip():
        return None

    # 计算在原文中的绝对位置
    absolute_start = last_ref_pos + len(after_ref) - len(remaining)
    return absolute_start


def has_holding_trigger(text: str, case_refs: list[CaseRef]) -> bool:
    """
    判断同一句中是否同时有 case_ref 和观点触发词。

    没有 case_ref 时直接返回 False——即使文本中有"法院认为""本院认为"。
    这是因为裁判文书正文大量出现这两个词，它们指当前文书自身。

    Args:
        text: 完整文本
        case_refs: 已提取的案例引用列表

    Returns:
        True 如果同时有 case_ref 和观点触发词
    """
    if not case_refs:
        return False
    return find_holding_trigger_position(text, case_refs) is not None
