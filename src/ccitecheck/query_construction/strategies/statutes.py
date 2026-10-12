"""法规 provider 检索策略。"""

from ...domain.queries import NormalizedLocator, ProviderStrategy, SourcePlanItem


def statute_strategies(
    plan: list[SourcePlanItem], locator: NormalizedLocator | None
) -> list[ProviderStrategy]:
    exact = bool(locator and locator.article_raw)
    return [
        ProviderStrategy(
            source_id=item.source_id,
            goal="retrieve_cited_article" if exact else "retrieve_relevant_articles",
            preferred_mode="exact_article" if exact else "law_semantic",
            allowed_fallbacks=(
                ["semantic_exact", "law_keyword"] if exact else ["law_keyword"]
            ),
        )
        for item in plan
    ]


__all__ = ["statute_strategies"]
