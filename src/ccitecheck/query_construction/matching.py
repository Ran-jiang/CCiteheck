"""北大法宝法规候选的确定性同名匹配。"""

from __future__ import annotations

import re
from typing import Protocol, TypeVar

from ..infrastructure.database import normalize_title, strip_version_annotation
from ..domain.law_titles import cn_title_shape_key, cn_title_shape_variants


class TitledRecord(Protocol):
    title: str


RecordT = TypeVar("RecordT", bound=TitledRecord)

_ISSUING_AUTHORITY_PREFIXES = (
    "中华人民共和国最高人民法院、中华人民共和国最高人民检察院",
    "中华人民共和国最高人民法院中华人民共和国最高人民检察院",
    "最高人民法院、最高人民检察院、公安部",
    "最高人民法院最高人民检察院公安部",
    "最高人民法院、最高人民检察院",
    "最高人民法院最高人民检察院",
    "中华人民共和国最高人民法院",
    "中华人民共和国最高人民检察院",
    "最高人民法院",
    "最高人民检察院",
)

# 法宝用纯年份后缀区分同名法的不同版本（如"…国家安全法(2015)"），
# strip_version_annotation 只认"修正/修订/修改"，此处补齐纯年份形态。
_BARE_VERSION = re.compile(
    r"[（(](?:\d{4}年?(?:第[一二三四五六七八九十0-9]+次)?(?:修正|修订|修改)?|修正|修订|修改)[）)]$"
)
_CURRENT_MARKER = "现行有效"
_REPEALED_MARKERS = ("废止", "失效")


def _base_title(title: str) -> str:
    return _BARE_VERSION.sub("", normalize_title(title or ""))


def normalize_law_title_for_comparison(title: str) -> str:
    """统一法名格式并剥离司法发布机关前缀，供身份比较使用。"""
    normalized = _normalized_title(title)
    return next(
        (
            normalized.removeprefix(prefix)
            for prefix in _ISSUING_AUTHORITY_PREFIXES
            if normalized.startswith(prefix)
        ),
        normalized,
    )


def equivalent_law_titles(left: str, right: str) -> bool:
    """判断两个法名是否仅有格式、国家全称或发布机关前缀差异。"""
    left_normalized = normalize_law_title_for_comparison(left)
    right_normalized = normalize_law_title_for_comparison(right)
    if left_normalized == right_normalized:
        return True
    return cn_title_shape_key(left_normalized) == cn_title_shape_key(right_normalized)


def matches_requested_law_title(requested: str, returned: str) -> bool:
    """同名之外，显式年份版本也必须一致；裸名仍可命中现行版本。"""
    if not equivalent_law_titles(requested, returned):
        return False
    requested_year = re.search(r"[（(]((?:19|20)\d{2})", normalize_title(requested))
    if requested_year is None:
        return True
    returned_year = re.search(r"[（(]((?:19|20)\d{2})", normalize_title(returned))
    return returned_year is not None and returned_year.group(1) == requested_year.group(1)


def _is_current(record: TitledRecord) -> bool:
    values = getattr(record, "timeliness", None) or []
    return any(_CURRENT_MARKER in value for value in values) and not any(
        marker in value for value in values for marker in _REPEALED_MARKERS
    )


def match_law_record(law_title: str, records: list[RecordT]) -> RecordT | None:
    """匹配同名法规及作为规范性文件发布载体的印发/发布通知。"""
    # 同一法名对应多个版本（旧版废止、新版现行）时，裸名引用指向现行版本，
    # 优先返回唯一的现行有效版本，避免把重新制定后仍在施行的法误判为废止。
    base_target = _base_title(law_title)
    base_variants = set(cn_title_shape_variants(base_target))
    same_base = [
        record for record in records
        if _base_title(record.title) in base_variants
    ]
    if len(same_base) > 1:
        current = [record for record in same_base if _is_current(record)]
        if len(current) == 1:
            return current[0]

    exact_target = _normalized_exact_title(law_title)
    exact_matches = [
        record for record in records
        if _normalized_exact_title(record.title) == exact_target
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]

    target = normalize_law_title_for_comparison(law_title)
    target_variants = set(cn_title_shape_variants(target))
    matches: list[RecordT] = []
    for record in records:
        candidate = normalize_law_title_for_comparison(record.title)
        if candidate in target_variants:
            matches.append(record)
    if len(matches) == 1:
        return matches[0]
    if matches:
        return None
    # 部门规范性文件可能只存在于印发/发布通知中，没有独立同名条目。
    # 仅接受书名号内嵌完整目标法名的发布载体。
    for record in records:
        title = record.title or ""
        if f"《{target}》" in title and ("印发" in title or "发布" in title):
            return record
    return None


def _normalized_title(title: str) -> str:
    # 先剥"修正/修订"注记，再剥纯年份版本后缀（法宝用"(2015)"区分同名版本），
    # 使裸名引用能匹配到带版本后缀的条文记录。
    return _BARE_VERSION.sub("", strip_version_annotation(normalize_title(title)))


def _normalized_exact_title(title: str) -> str:
    return normalize_title(title).translate(str.maketrans({"（": "(", "）": ")"}))


__all__ = [
    "equivalent_law_titles",
    "matches_requested_law_title",
    "match_law_record",
    "normalize_law_title_for_comparison",
]
