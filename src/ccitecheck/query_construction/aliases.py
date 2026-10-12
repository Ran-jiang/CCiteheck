"""文内简称候选。"""

from __future__ import annotations

from ..domain.citation import AliasDeclaration
from ..infrastructure.database import normalize_title


def alias_map(declarations: list[AliasDeclaration]) -> dict[str, str]:
    return {
        normalize_title(item.alias_raw): item.full_name_raw
        for item in declarations
    }


__all__ = ["alias_map"]
