"""案例 provider 检索策略。"""

from ...domain.queries import ProviderStrategy, SourcePlanItem


def case_strategies(plan: list[SourcePlanItem]) -> list[ProviderStrategy]:
    return [
        ProviderStrategy(
            source_id=item.source_id,
            goal="retrieve_cited_case",
            preferred_mode="case_exact",
            allowed_fallbacks=["case_semantic"],
        )
        for item in plan
    ]


__all__ = ["case_strategies"]
