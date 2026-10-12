"""按法域生成数据源宏观计划；执行顺序由 Scheduler 消费。"""

from ..domain.queries import SourcePlanItem
from .jurisdiction import JURISDICTION_CN, JURISDICTION_EU, JURISDICTION_UNKNOWN


def plan_sources(jurisdiction: str) -> list[SourcePlanItem]:
    if jurisdiction == JURISDICTION_EU:
        # EUR-Lex 为主，Ansvar 作补充检索/复核
        return [
            SourcePlanItem(source_id="eurlex", priority=1, reason="eu_statute"),
            SourcePlanItem(source_id="ansvar", priority=2, reason="ansvar_supplement"),
        ]
    if jurisdiction == JURISDICTION_CN:
        return [
            SourcePlanItem(source_id="local_laws", priority=1, reason="local_authoritative_copy"),
            SourcePlanItem(source_id="pkulaw", priority=2, reason="authoritative_fallback"),
        ]
    if jurisdiction in {JURISDICTION_UNKNOWN, "FOREIGN"}:
        # 法域不明 → 不自动检索，保留人工核查入口
        return []
    # 具体涉外法域（DE/FR/JP/GB/US-CA…）→ Ansvar 对应法域语料
    return [SourcePlanItem(source_id="ansvar", priority=1, reason="ansvar_foreign_jurisdiction")]


__all__ = ["plan_sources"]
