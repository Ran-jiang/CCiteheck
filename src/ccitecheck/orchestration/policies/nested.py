"""Scheduler 的 nested parent dependency 策略。"""

from __future__ import annotations

from typing import Protocol

from ...domain.statute_results import StatuteFinding


class NestedDependencyItem(Protocol):
    parent_index: int | None
    relation_status: str | None
    relation_message: str


def finalize_nested_dependencies(
    items: list[NestedDependencyItem],
    judgments: dict[int, list[StatuteFinding]],
) -> None:
    for index, item in enumerate(items):
        if item.parent_index is None or item.relation_status not in {"confirmed", "resolved"}:
            continue
        if not judgments[item.parent_index]:
            continue
        item.relation_status = "parent_failed"
        item.relation_message = (
            "该条属于主法条中的内部转引；主引用当前未通过或未完成核验，"
            "本条随主引用处理，不单独作出通过或未通过结论。"
        )
        judgments[index] = []


__all__ = ["finalize_nested_dependencies"]
