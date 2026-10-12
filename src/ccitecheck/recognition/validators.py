"""
CCiteheck 引用文档校验。

validate_claim_document 对 ClaimDocument 执行全量不变量校验。
校验失败返回违反项列表（字符串），通过返回空列表。

校验断言清单：
  - claim_id 唯一、按序连续
  - claim.text 非空，且严格等于 anchor_ids 文本拼接
  - anchor_ids 非空、全部存在、编号连续
  - claim_type 合法
  - entities 与 claim_type 的对应关系由 Claim 模型校验
"""

from __future__ import annotations

from ..domain.document import Anchor, Block, BlockRelationType, ParsedDocument

from ..domain.citation import ClaimDocument, ClaimType
from ..domain.citation_integrity import validate_claim_citation_integrity
from .anchor_text import parse_anchor_number, rebuild_anchor_text


def _parse_anchor_number(anchor_id: str) -> int:
    """从 anchor 编号提取数字部分"""
    return parse_anchor_number(anchor_id)


def _check_anchor_continuity(anchor_ids: list[str]) -> bool:
    """检查 anchor 编号是否连续"""
    if not anchor_ids:
        return False
    nums = [_parse_anchor_number(aid) for aid in anchor_ids]
    if any(n < 0 for n in nums):
        return False
    nums.sort()
    for i in range(1, len(nums)):
        if nums[i] != nums[i - 1] + 1:
            return False
    return True


def validate_claim_document(
    parsed_doc: ParsedDocument,
    claim_doc: ClaimDocument,
) -> list[str]:
    """
    对 ClaimDocument 执行全量不变量校验。

    Args:
        parsed_doc: 来源 ParsedDocument
        claim_doc: 待校验的 ClaimDocument

    Returns:
        违反项字符串列表。空列表表示全部校验通过。
    """
    violations: list[str] = []

    # 构建索引
    anchor_map: dict[str, Anchor] = {a.anchor: a for a in parsed_doc.anchors}
    block_map: dict[str, Block] = {b.block_id: b for b in parsed_doc.blocks}

    # ---- 1. claim_id 唯一 ----
    claim_ids = [c.claim_id for c in claim_doc.claims]
    if len(claim_ids) != len(set(claim_ids)):
        violations.append("[claim_id] claim_id 重复")
        # 无法继续校验唯一性相关项
        return violations

    # ---- 2. claim_id 按序连续 ----
    for i, c in enumerate(claim_doc.claims):
        expected_id = f"cl_{i + 1:05d}"
        if c.claim_id != expected_id:
            violations.append(
                f"[claim_id] claim_id 不连续: 期望 {expected_id}, 实际 {c.claim_id}"
            )

    # ---- 3. 逐条校验 ----
    for claim in claim_doc.claims:
        # claim.text 非空
        if not claim.text:
            violations.append(
                f"[text] claim {claim.claim_id}: text 为空"
            )

        # claim.text 严格等于 anchor_ids 文本拼接
        # （与设计决策 2.1 对应：不改写原文由结构保证）
        expected_text = _rebuild_from_anchors(claim.anchor_ids, anchor_map)
        if claim.text != expected_text:
            violations.append(
                f"[text] claim {claim.claim_id}: text 与 anchor 拼接不一致. "
                f"claim.text={repr(claim.text)[:80]}, "
                f"expected={repr(expected_text)[:80]}"
            )

        # anchor_ids 非空
        if not claim.anchor_ids:
            violations.append(
                f"[anchor] claim {claim.claim_id}: anchor_ids 为空"
            )

        # anchor 全部存在
        for aid in claim.anchor_ids:
            if aid not in anchor_map:
                violations.append(
                    f"[anchor] claim {claim.claim_id}: anchor {aid} 不存在"
                )

        # anchor 编号连续
        if (
            claim.anchor_ids
            and not _check_anchor_continuity(claim.anchor_ids)
            and not _is_related_table_pair(claim.anchor_ids, anchor_map, block_map)
        ):
            violations.append(
                f"[anchor] claim {claim.claim_id}: anchor_ids 编号不连续: "
                f"{claim.anchor_ids}"
            )

        # claim_type 合法
        if not isinstance(claim.claim_type, ClaimType):
            violations.append(
                f"[claim_type] claim {claim.claim_id}: claim_type 非法: "
                f"{claim.claim_type}"
            )

        for source in getattr(claim.entities, "legal_sources", []):
            inherited = source.recognition.inherited_from
            if source.recognition.form == "inherited" and inherited is None:
                violations.append(
                    f"[entities] claim {claim.claim_id}: "
                    f"承前法源 {source.title} 缺少 inherited_from"
                )
            if inherited and inherited.anchor_id and inherited.anchor_id not in anchor_map:
                violations.append(
                    f"[entities] claim {claim.claim_id}: "
                    f"inherited_from.anchor_id {inherited.anchor_id} 不存在"
                )

        for violation in validate_claim_citation_integrity(claim):
            violations.append(
                f"[citation] claim {claim.claim_id}: {violation}"
            )

    return violations


def _is_related_table_pair(anchor_ids, anchor_map, block_map) -> bool:
    if len(anchor_ids) != 2:
        return False
    source_anchor = anchor_map.get(anchor_ids[0])
    content_anchor = anchor_map.get(anchor_ids[1])
    source = block_map.get(source_anchor.block_id) if source_anchor else None
    content = block_map.get(content_anchor.block_id) if content_anchor else None
    return bool(
        source
        and content
        and source.type.value == "table_cell"
        and content.type.value == "table_cell"
        and any(
            relation.relation_type == BlockRelationType.TABLE_LEFT
            and relation.target_block_id == source.block_id
            for relation in content.relations
        )
    )


def _rebuild_from_anchors(
    anchor_ids: list[str],
    anchor_map: dict[str, Anchor],
) -> str:
    """从 anchors 重建文本（用于校验）"""
    return rebuild_anchor_text(anchor_ids, anchor_map)
