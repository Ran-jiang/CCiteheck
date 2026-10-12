"""内部转引关系的权威证据比较。"""

from __future__ import annotations

from typing import Protocol

from ...domain.citation import ArticleRef, Claim
from ...domain.evidence import SourceTrace
from ...infrastructure.database import normalize_article_key, normalize_title
from ...recognition.relations import relation_candidates
from ...recognition.statutes import ARTICLE_PATTERN


class NestedItem(Protocol):
    claim: Claim
    law_title: str
    display_title: str
    article: ArticleRef | None
    article_no: str | None
    parent_index: int | None
    relation_status: str | None
    relation_message: str
    relation_candidate_article_no: str | None
    relation_parent_authoritative_text: str
    citation_span: tuple[int, int] | None
    reference_role: str

    @property
    def lookup_key(self) -> tuple: ...


def resolve_nested_relations(
    items: list[NestedItem],
    lookup_results: dict[tuple, tuple[object, list[SourceTrace]]],
) -> None:
    """以主法条权威原文中明确出现的条号确认内部转引。"""
    candidates = relation_candidates(items)
    for child_index, parent_indices in candidates.items():
        child = items[child_index]
        unavailable_parent: int | None = None
        insufficient: tuple[int, str] | None = None
        for parent_index in parent_indices:
            parent = items[parent_index]
            parent_lookup = lookup_results.get(parent.lookup_key)
            if parent_lookup is None or parent_lookup[0].evidence is None:
                unavailable_parent = parent_index
                continue
            parent_text = parent_lookup[0].evidence.article_text or ""
            if not parent_text:
                unavailable_parent = parent_index
                continue
            if not _parent_could_reference_child(parent_text, parent, child):
                continue
            mentions = {
                normalize_article_key(match.group()): match.group()
                for match in ARTICLE_PATTERN.finditer(parent_text)
            }
            cited = normalize_article_key(child.article_no or "")
            if cited in mentions:
                _confirm(
                    child, parent, parent_index, parent_text, "confirmed",
                    f"主法条原文明示转引{mentions[cited]}", mentions[cited],
                )
                break
            if len(mentions) != 1:
                insufficient = (parent_index, "主法条原文包含多个转引条号，无法唯一对应")
                continue
            candidate = next(iter(mentions.values()))
            _confirm(
                child, parent, parent_index, parent_text, "locator_mismatch",
                f"主法条原文明示转引{candidate}，与所引{child.article_no or '条号'}不一致",
                candidate,
            )
            break
        else:
            if insufficient is not None:
                parent_index, reason = insufficient
                parent = items[parent_index]
                evidence = lookup_results[parent.lookup_key][0].evidence
                _confirm(
                    child, parent, parent_index,
                    evidence.article_text if evidence else "",
                    "insufficient", reason,
                )
            elif unavailable_parent is not None:
                parent = items[unavailable_parent]
                _confirm(
                    child, parent, unavailable_parent, "", "parent_unavailable",
                    "主引用原文不可核验，无法确认转引关系",
                )


def _parent_could_reference_child(
    parent_text: str, parent: NestedItem, child: NestedItem,
) -> bool:
    if not ARTICLE_PATTERN.search(parent_text):
        return False
    if normalize_title(parent.law_title) == normalize_title(child.law_title):
        return True
    normalized_parent = normalize_title(parent_text)
    variants = {
        normalize_title(child.display_title),
        normalize_title(child.law_title),
        normalize_title(child.law_title).removeprefix("中华人民共和国"),
    }
    return any(variant and variant in normalized_parent for variant in variants)


def _confirm(
    child: NestedItem,
    parent: NestedItem,
    parent_index: int,
    parent_text: str,
    status: str,
    message: str,
    candidate_article_no: str | None = None,
) -> None:
    child.parent_index = parent_index
    child.relation_status = status
    child.relation_message = message
    child.relation_candidate_article_no = candidate_article_no
    child.relation_parent_authoritative_text = parent_text
    child.reference_role = "nested"


__all__ = ["resolve_nested_relations"]
