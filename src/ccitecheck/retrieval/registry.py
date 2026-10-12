"""已配置检索来源注册表。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .sources.ansvar import AnsvarSource
from .sources.base import StatuteSource
from .sources.eurlex import EurLexSource
from .sources.local_laws import LocalSQLiteSource
from .sources.pkulaw.cases import CaseSearcher, PkulawCaseSource
from .sources.pkulaw.statutes import PkulawFallbackSource

SourceAdapter = StatuteSource | CaseSearcher


@dataclass(frozen=True)
class SourceRegistry:
    sources: dict[str, SourceAdapter]

    @classmethod
    def default(cls, law_db: str | Path) -> "SourceRegistry":
        return cls({
            "local_laws": LocalSQLiteSource(law_db),
            "pkulaw": PkulawFallbackSource(),
            "pkulaw_cases": PkulawCaseSource(),
            "eurlex": EurLexSource(),
            "ansvar": AnsvarSource(),
        })

    def get(self, source_id: str) -> SourceAdapter | None:
        return self.sources.get(source_id)


__all__ = ["SourceRegistry"]
