"""为同一原文范围生成跨引用类型的稳定展示分组。"""

from __future__ import annotations

import hashlib

from .citation import Claim


def display_group_id(claim: Claim) -> str:
    if claim.note_context and claim.note_context.reference_anchor_id:
        source_key = claim.note_context.reference_anchor_id
    else:
        source_key = "|".join(claim.anchor_ids)
    digest = hashlib.sha1(source_key.encode("utf-8")).hexdigest()[:12]
    return f"dg_{digest}"


__all__ = ["display_group_id"]
