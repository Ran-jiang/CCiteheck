"""仅依据原文 span 识别 nested relation candidates。"""

from __future__ import annotations

import re
from typing import Protocol


class RelationItem(Protocol):
    claim: object
    article: object | None
    citation_span: tuple[int, int] | None


_PARENT_REPORTING = re.compile(
    r"^\s*(?:的|之)?(?:明确)?(?:规定|指出|载明|所称)"
)


def relation_candidates(items: list[RelationItem]) -> dict[int, list[int]]:
    result: dict[int, list[int]] = {}
    by_claim: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        if item.article is not None and item.citation_span is not None:
            by_claim.setdefault(item.claim.claim_id, []).append(index)
    for indices in by_claim.values():
        indices.sort(key=lambda index: items[index].citation_span[0])
        for position, child_index in enumerate(indices[1:], start=1):
            child_start = items[child_index].citation_span[0]
            parents: list[int] = []
            for parent_index in reversed(indices[:position]):
                parent = items[parent_index]
                bridge = parent.claim.text[parent.citation_span[1]:child_start]
                if "\n" in bridge or len(bridge) > 1200:
                    break
                if _bridge_stays_in_parent_scope(bridge):
                    parents.append(parent_index)
            if parents:
                result[child_index] = parents
    return result


def _bridge_stays_in_parent_scope(bridge: str) -> bool:
    reporting = _PARENT_REPORTING.match(bridge)
    if reporting is None:
        return False
    governed = bridge[reporting.end():]
    last_open = max(governed.rfind(mark) for mark in "“‘")
    last_close = max(governed.rfind(mark) for mark in "”’")
    return last_open > last_close or re.search(r"[。！？]", governed) is None


__all__ = ["relation_candidates"]
