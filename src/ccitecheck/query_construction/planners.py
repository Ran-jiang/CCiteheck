"""Query Construction 的确定性 Primary Planner 与 LLM Planner 边界。"""

from __future__ import annotations

import difflib
import re
from typing import Protocol

from ..domain.claims import RawClaim, RawLegalMention
from ..domain.queries import (
    IdentityCandidate,
    NormalizedLocator,
    RepairPlan,
    RetrievalQueryPlan,
)
from ..infrastructure.database import normalize_title
from ..domain.law_titles import cn_title_shape_key
from .versioning import explicit_version_hint


_SUBSTANTIVE_RULE = re.compile(
    r"(?:规定|明确|指出|载明|要求)[：:，,\s]*[^。；]{10,}"
)


class RelatedPlanner(Protocol):
    def plan_related(
        self, *, raw_text: str, raw_title: str, raw_time: str | None,
        target_name: str,
    ) -> RetrievalQueryPlan: ...


def primary_plan(
    claim: RawClaim,
    mention: RawLegalMention | None,
    identities: list[IdentityCandidate],
    locator: NormalizedLocator | None,
    planner: RelatedPlanner | None = None,
) -> tuple[RetrievalQueryPlan, str | None]:
    """先做所有确定性决定；只有无条号规范性文件才调用 Related Planner。"""
    if claim.case_mentions:
        case = claim.case_mentions[0]
        return RetrievalQueryPlan(
            route="case",
            target_name=case.raw_case_number or case.raw_case_name,
        ), None
    if mention is None or not identities:
        return RetrievalQueryPlan(route="skip"), None

    target = next(
        (item.title for item in reversed(identities) if item.basis != "raw"),
        identities[0].title,
    )
    version_hint = explicit_version_hint(mention.raw_time)
    fallback_query = claim.raw_text if _SUBSTANTIVE_RULE.search(claim.raw_text) else None
    if locator and locator.article_raw:
        return RetrievalQueryPlan(
            route="statute_exact",
            target_name=target,
            article_no=locator.article_raw,
            version_hint=version_hint,
        ), None
    if planner is None or not callable(getattr(planner, "plan_related", None)):
        return RetrievalQueryPlan(
            route="statute_related",
            target_name=target,
            query_text=fallback_query,
            version_hint=version_hint,
        ), "related_planner_not_configured"
    try:
        plan = RetrievalQueryPlan.model_validate(planner.plan_related(
            raw_text=claim.raw_text,
            raw_title=mention.raw_title or mention.raw_title_candidate or "",
            raw_time=mention.raw_time,
            target_name=target,
        ))
    except Exception as exc:
        return RetrievalQueryPlan(
            route="statute_related",
            target_name=target,
            query_text=fallback_query,
            version_hint=version_hint,
        ), f"related_planner_failed: {exc}"
    if plan.route != "statute_related":
        return RetrievalQueryPlan(
            route="statute_related",
            target_name=target,
            query_text=fallback_query,
            version_hint=version_hint,
        ), "related_planner_returned_invalid_route"
    return plan.model_copy(update={
        "target_name": target,
        "article_no": None,
        "query_text": plan.query_text or fallback_query,
        "version_hint": version_hint or plan.version_hint,
    }), None


def fuzzy_title_candidates(raw_title: str, titles: list[str], limit: int = 5) -> list[str]:
    normalized = {normalize_title(title): title for title in titles if title}
    matches = difflib.get_close_matches(
        normalize_title(raw_title), list(normalized), n=limit, cutoff=0.65
    )
    return [normalized[item] for item in matches]


def repair_target_is_allowed(
    plan: RepairPlan,
    raw_title: str,
    candidate_titles: list[str],
    fuzzy_candidates: list[str],
) -> bool:
    retry = plan.retry_request
    if retry is None or not retry.target_name:
        return True
    allowed = {
        cn_title_shape_key(normalize_title(item))
        for item in [raw_title, *candidate_titles, *fuzzy_candidates]
        if item
    }
    return cn_title_shape_key(normalize_title(retry.target_name)) in allowed

__all__ = [
    "RelatedPlanner",
    "fuzzy_title_candidates",
    "primary_plan",
    "repair_target_is_allowed",
]
