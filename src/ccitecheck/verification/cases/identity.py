"""已确认案例的引用信息确定性比对。"""

from __future__ import annotations

import re

from ...domain.case_results import CaseErrorCode, CaseFinding
from ...domain.evidence import CaseEvidence

_CASE_TITLE_SUFFIX = re.compile(
    r"(?:(?:一审|二审|再审|终审|再审审查)?(?:民事|刑事|行政)?"
    r"(?:判决书|裁定书|调解书|决定书|通知书|支付令)|纠纷案|案)+$"
)
_MUNICIPALITIES = {"北京", "上海", "天津", "重庆"}
_CASE_CONTEXT_PREFIX = re.compile(r"^(?:在|根据|依据|依照|参见|援引|可参考|参考|参照)+")


def compare_case_identity(
    *,
    cited_case_number: str | None,
    cited_case_name: str | None,
    cited_court: str | None,
    evidence: CaseEvidence,
) -> CaseFinding | None:
    """比较文书明确写出的身份字段；未写出的字段不参与判断。"""
    differences: list[str] = []
    if cited_case_number and normalize_case_number(cited_case_number) != normalize_case_number(evidence.case_number):
        differences.append(f"案号应为{evidence.case_number}")
    if cited_case_name and not equivalent_case_name(cited_case_name, evidence.title):
        differences.append(f"案名应为《{evidence.title}》")
    if cited_court and not same_court(cited_court, evidence.court):
        differences.append(f"审理法院应为{evidence.court}")
    if not differences:
        return None
    return CaseFinding(
        code=CaseErrorCode.CASE_IDENTITY_ERROR,
        risk_level="HIGH",
        summary="；".join(differences),
        suggestion=f"{'；'.join(differences)}，请按北大法宝权威记录更正。",
    )


def normalize_case_number(value: str) -> str:
    table = str.maketrans({"（": "(", "）": ")", "〔": "(", "〕": ")", "　": "", " ": ""})
    return value.translate(table).lower()


def normalize_case_name(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", value)
    normalized = _CASE_CONTEXT_PREFIX.sub("", normalized)
    normalized = normalized.replace("指导性案例", "指导案例")
    return _CASE_TITLE_SUFFIX.sub("", normalized)


def case_search_title(value: str) -> str:
    """生成供应商可检索的案名，不改变领域层的统一名称。"""
    cleaned = _CASE_CONTEXT_PREFIX.sub("", value.strip())
    guiding = guiding_case_id(normalize_case_name(cleaned))
    return f"指导性案例{guiding}号" if guiding else cleaned


def case_parties(value: str) -> tuple[str, str] | None:
    """从常见的“A诉B案”引用中提取双方当事人检索词。"""
    cleaned = normalize_case_name(value)
    match = re.fullmatch(r"(.{1,30}?)诉(.{1,30})", cleaned)
    if not match:
        return None
    return match.group(1), match.group(2)


def equivalent_case_name(cited: str, authoritative: str) -> bool:
    normalized_cited = normalize_case_name(cited)
    normalized_authoritative = normalize_case_name(authoritative)
    if normalized_cited == normalized_authoritative:
        return True
    if (
        len(normalized_cited) >= 8
        and "典型案例" in authoritative
        and normalized_cited in normalized_authoritative
    ):
        return True
    cited_guiding_id = guiding_case_id(normalized_cited)
    if cited_guiding_id and cited_guiding_id == guiding_case_id(normalized_authoritative):
        return True
    parties = case_parties(cited)
    organization_tokens = ("公司", "集团", "法院", "委员会", "事务所", "中心", "协会")
    safe_parties = bool(parties) and all(
        1 < len(party) <= 6 and not any(token in party for token in organization_tokens)
        for party in parties or ()
    )
    return bool(safe_parties and all(party in normalized_authoritative for party in parties or ()))


def guiding_case_id(value: str) -> str | None:
    match = re.match(r"^指导案例(\d+)号", value)
    return match.group(1) if match else None


def normalize_court(value: str) -> str:
    compact = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", value)
    match = re.fullmatch(r"(.+?)(?:第)?([一二三四五六七八九十\d]+)(?:中院|中级法院|中级人民法院)", compact)
    if match:
        region, ordinal = match.groups()
        if region in _MUNICIPALITIES:
            region += "市"
        return f"{region}第{ordinal}中级人民法院"
    return compact


def same_court(left: str, right: str) -> bool:
    return bool(left and right and normalize_court(left) == normalize_court(right))


__all__ = [
    "compare_case_identity",
    "case_parties",
    "case_search_title",
    "equivalent_case_name",
    "guiding_case_id",
    "normalize_case_name",
    "normalize_case_number",
    "normalize_court",
    "same_court",
]
