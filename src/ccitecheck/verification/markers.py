"""Remove document-internal anchors from user-facing model text."""

from __future__ import annotations

import re

_INTERNAL_MARKERS = re.compile(
    r"⟦[^⟧]*⟧|\[\[[^\[\]]{0,60}\]\]|【(?:锚点|anchor)[^】]*】|(?<![A-Za-z])line\d{4,6}(?!\d)",
    re.IGNORECASE,
)


def strip_internal_markers(text: str) -> str:
    """Strip only the internal marker formats emitted by this application."""
    return _INTERNAL_MARKERS.sub("", text).strip()


__all__ = ["strip_internal_markers"]
