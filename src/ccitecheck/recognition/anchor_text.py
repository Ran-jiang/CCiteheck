"""Shared helpers for rendering claim text from document anchors."""

from __future__ import annotations

import re

from ..domain.document import Anchor


def parse_anchor_number(anchor_id: str) -> int:
    """Return the numeric part of an anchor ID, or -1 when it is invalid."""
    match = re.search(r"(\d+)", anchor_id)
    return int(match.group(1)) if match else -1


def rebuild_anchor_text(
    anchor_ids: list[str], anchor_map: dict[str, Anchor]
) -> str:
    """Render exact source text in numeric anchor order."""
    return "".join(
        anchor_map[anchor_id].text
        for anchor_id in sorted(anchor_ids, key=parse_anchor_number)
        if anchor_id in anchor_map
    )


__all__ = ["parse_anchor_number", "rebuild_anchor_text"]
