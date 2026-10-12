"""法规版本查询假设。"""

import re

from ..domain.queries import VersionHypothesis
from .normalization import split_version_annotation


_EXPLICIT_VERSION = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})\s*年?\s*(?P<kind>修正|修订|修改)"
)


def explicit_version_hint(raw_time: str | None) -> str | None:
    """只返回原文明确指向法规版本的时间表达。"""
    value = (raw_time or "").strip()
    if value == "现行":
        return "current"
    return value if _EXPLICIT_VERSION.search(value) else None


def build_version_hypothesis(
    title: str, raw_time: str | None = None
) -> VersionHypothesis | None:
    hint = explicit_version_hint(raw_time)
    if hint == "current":
        return VersionHypothesis(kind="current", temporal_reference=raw_time)
    if hint:
        match = _EXPLICIT_VERSION.search(hint)
        return VersionHypothesis(
            year=int(match.group("year")),
            kind="revised" if match.group("kind") == "修订" else "amended",
            temporal_reference=raw_time,
        )
    _, year, kind = split_version_annotation(title)
    if year is None:
        return None
    return VersionHypothesis(year=year, kind=kind or "unknown", temporal_reference=title)


__all__ = ["build_version_hypothesis", "explicit_version_hint"]
