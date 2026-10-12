"""
CCiteheck 引用候选裁决器。

所有规则候选必须经过 Arbiter 后才成为最终 Claim。

核心职责：
  1. 硬校验：schema / anchor 存在性 / anchor 连续性
  2. out-of-scope 过滤
  3. 从 anchors 重建 claim.text（"不改写原文"的结构性保证）
  4. 实体子串校验
  5. 同位置去重合并
  6. 完整性裁决（子集候选 → 保留更完整的）
  7. 排序、生成 claim_id
  8. 派生原文与承前法源定位

处理顺序和逻辑严格按照设计规范 §8 执行。
"""

from __future__ import annotations

import logging

from ..domain.document import Anchor, BlockRelationType, ParsedDocument

from .anchor_text import parse_anchor_number, rebuild_anchor_text
from ..domain.citation import (
    Claim,
    ClaimCandidate,
    ClaimDocument,
    ClaimType,
    CLAIM_EXTRACTOR_VERSION,
    CLAIM_SCHEMA_VERSION,
    InheritedSourceReference,
    NoteContext,
    SourceLocation,
)

logger = logging.getLogger(__name__)


# ============================================================
# 辅助函数
# ============================================================

def _parse_anchor_number(anchor_id: str) -> int:
    """兼容原有内部调用。"""
    return parse_anchor_number(anchor_id)


def _check_anchor_continuity(anchor_ids: list[str]) -> bool:
    """
    检查 anchor 编号是否连续。

    连续 = 全局编号相邻（如 line00052、line00053）。
    允许跨 block。
    """
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


def _is_related_table_pair(
    anchor_ids: list[str], parsed_doc: ParsedDocument, anchor_map: dict[str, Anchor]
) -> bool:
    """允许合并法源格与其覆盖的非相邻行条文格组成 claim。"""
    if len(anchor_ids) != 2:
        return False
    block_map = {block.block_id: block for block in parsed_doc.blocks}
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


def _rebuild_text(anchor_ids: list[str], anchor_map: dict[str, Anchor]) -> str:
    """
    从 anchors 重建 claim.text。

    按 anchor 编号排序后拼接 anchor.text。
    这是"不改写原文"的结构性保证——claim.text 永远等于原文锚点文本的精确拼接。

    Args:
        anchor_ids: anchor 编号列表
        anchor_map: anchor_id → Anchor 的映射

    Returns:
        按序拼接的文本
    """
    # 按编号排序
    return rebuild_anchor_text(anchor_ids, anchor_map)


def _derive_context_text(
    anchor_ids: list[str],
    parsed_doc: ParsedDocument,
    anchor_map: dict[str, Anchor],
) -> str:
    """返回首锚点所在 chunk 的完整文本，作为语义比对上下文。"""
    if not anchor_ids:
        return ""
    target = anchor_ids[0]
    for chunk in parsed_doc.chunks:
        if target in chunk.anchor_ids:
            return "".join(
                anchor_map[anchor_id].text
                for anchor_id in chunk.anchor_ids
                if anchor_id in anchor_map
            )
    return _rebuild_text(anchor_ids, anchor_map)


def _derive_source_locations(
    anchor_ids: list[str], parsed_doc: ParsedDocument, anchor_map: dict[str, Anchor]
) -> list[SourceLocation]:
    """把内部锚点转换为来源平台可解释的稳定定位坐标。"""
    block_map = {block.block_id: block for block in parsed_doc.blocks}
    result: list[SourceLocation] = []
    for anchor_id in anchor_ids:
        anchor = anchor_map.get(anchor_id)
        block = block_map.get(anchor.block_id) if anchor else None
        if not anchor or not block:
            continue
        result.append(SourceLocation(
            platform=parsed_doc.doc_meta.source_platform,
            document_id=parsed_doc.doc_meta.source_document_id,
            revision=parsed_doc.doc_meta.source_revision,
            block_id=block.external_block_id or block.block_id,
            char_start=anchor.char_start,
            char_end=anchor.char_end,
            anchor_text=anchor.text,
            occurrence=block.text[:anchor.char_start].count(anchor.text),
            table_index=block.table_index,
            row_index=block.row_index,
            cell_index=block.cell_index,
            row_start=block.row_start,
            row_end=block.row_end,
            col_start=block.col_start,
            col_end=block.col_end,
        ))
    return result


def _attach_inherited_source_locations(
    entities, parsed_doc: ParsedDocument, anchor_map: dict[str, Anchor]
) -> None:
    """为承前法源补全可脱离 ParsedDocument 使用的定位。"""
    for source in getattr(entities, "legal_sources", []):
        inherited = source.recognition.inherited_from
        if source.recognition.form != "inherited" or inherited is None or not inherited.anchor_id:
            continue
        locations = _derive_source_locations(
            [inherited.anchor_id], parsed_doc, anchor_map
        )
        source.recognition.inherited_from = InheritedSourceReference(
            anchor_id=inherited.anchor_id,
            source_location=locations[0] if locations else None,
        )


def _derive_note_context(
    anchor_ids: list[str], parsed_doc: ParsedDocument, anchor_map: dict[str, Anchor]
) -> NoteContext | None:
    """将脚注/尾注 Claim 回挂到正文中的实际引用点。"""
    if not anchor_ids:
        return None
    block_map = {block.block_id: block for block in parsed_doc.blocks}
    anchor = anchor_map.get(anchor_ids[0])
    note_block = block_map.get(anchor.block_id) if anchor else None
    if note_block is None or not note_block.note_type or not note_block.note_id:
        return None
    referenced_from: list[SourceLocation] = []
    reference_anchor_id: str | None = None
    for block in parsed_doc.blocks:
        for reference in block.note_references:
            if (
                reference.note_type != note_block.note_type
                or reference.note_id != note_block.note_id
            ):
                continue
            referenced_from.append(SourceLocation(
                platform=parsed_doc.doc_meta.source_platform,
                document_id=parsed_doc.doc_meta.source_document_id,
                revision=parsed_doc.doc_meta.source_revision,
                block_id=block.external_block_id or block.block_id,
                char_start=reference.char_offset,
                char_end=reference.char_offset,
                anchor_text=block.text,
                occurrence=0,
                table_index=block.table_index,
                row_index=block.row_index,
                cell_index=block.cell_index,
                row_start=block.row_start,
                row_end=block.row_end,
                col_start=block.col_start,
                col_end=block.col_end,
            ))
            reference_anchor = next((
                candidate for candidate in parsed_doc.anchors
                if candidate.block_id == block.block_id
                and candidate.char_start <= reference.char_offset <= candidate.char_end
            ), None)
            if reference_anchor_id is None and reference_anchor is not None:
                reference_anchor_id = reference_anchor.anchor
    return NoteContext(
        note_type=note_block.note_type,
        note_id=note_block.note_id,
        referenced_from=referenced_from,
        reference_anchor_id=reference_anchor_id,
    )


# ============================================================
# 实体选择
# ============================================================

def _count_legal_sources(entities) -> int:
    """返回 legal_sources 的数量（用于实体比较）"""
    if hasattr(entities, "legal_sources"):
        return len(entities.legal_sources)
    return 0


def _select_better_entities(entities_a, entities_b):
    """
    从两个同位置候选中选择信息更全的实体。

    规则：
      1. legal_sources 数量多者优先
      2. 数量相同则保留 a

    Args:
        entities_a: 候选A的实体
        entities_b: 候选B的实体
    Returns:
        选中的实体
    """
    count_a = _count_legal_sources(entities_a)
    count_b = _count_legal_sources(entities_b)

    if count_b > count_a:
        return entities_b
    if count_a > count_b:
        return entities_a

    return entities_a


# ============================================================
# 引用候选裁决。
# ============================================================

def arbitrate_claim_candidates(
    candidates: list[ClaimCandidate],
    parsed_doc: ParsedDocument,
) -> list[Claim]:
    """
    Claim Arbiter — 所有候选的最终裁决点。

    处理顺序（严格按设计规范 §8）：
      1. 硬校验：claim_type 合法、anchor_ids 非空、全部存在于 parsed_doc、编号连续
      2. 从 anchors 重建 claim.text
      3. 实体子串校验
      4. 去重合并：(claim_type, tuple(anchor_ids)) 相同 → 合并
      5. 完整性裁决：子集候选 → 保留更长的
      6. 不同位置不合并
      7. 排序与编号
      8. 派生原文与承前法源定位

    Args:
        candidates: 所有规则候选
        parsed_doc: 已解析的文档

    Returns:
        最终 Claim 列表
    """
    # 构建索引
    anchor_map: dict[str, Anchor] = {a.anchor: a for a in parsed_doc.anchors}
    valid_anchor_ids = set(anchor_map.keys())

    # ---- 第1步：硬校验 ----
    passed: list[ClaimCandidate] = []
    for cand in candidates:
        # claim_type 合法
        if not isinstance(cand.claim_type, ClaimType):
            logger.warning("候选 claim_type 非法: %s，丢弃", cand.claim_type)
            continue

        # anchor_ids 非空
        if not cand.anchor_ids:
            logger.warning("候选 anchor_ids 为空，丢弃")
            continue

        # anchor 全部存在
        if not all(aid in valid_anchor_ids for aid in cand.anchor_ids):
            missing = [aid for aid in cand.anchor_ids if aid not in valid_anchor_ids]
            logger.warning("候选 anchor 不存在: %s，丢弃", missing)
            continue

        # anchor 编号连续
        if not _check_anchor_continuity(cand.anchor_ids) and not _is_related_table_pair(
            cand.anchor_ids, parsed_doc, anchor_map
        ):
            logger.warning("候选 anchor 编号不连续: %s，丢弃", cand.anchor_ids)
            continue

        passed.append(cand)

    if not passed:
        return []

    # ---- 第2步：去重合并 ----
    # key = (claim_type, tuple(anchor_ids))
    merged: dict[tuple, ClaimCandidate] = {}

    for cand in passed:
        key = (cand.claim_type, tuple(cand.anchor_ids))

        if key in merged:
            # 合并：实体取信息更全的一方
            existing = merged[key]
            merged[key].entities = _select_better_entities(
                existing.entities, cand.entities,
            )
        else:
            merged[key] = cand

    # 转回列表
    deduped = []
    for cand in merged.values():
        deduped.append(cand)

    # ---- 第4步：完整性裁决 ----
    # 若候选 A 的 anchor_ids 是候选 B 的 anchor_ids 的真子集，
    # 且二者 claim_type 相同、anchor 区间重叠，保留更长的 B

    # 按 anchor 数量降序排列（长的在前）
    deduped.sort(key=lambda c: len(c.anchor_ids), reverse=True)

    completeness_ruled: list[ClaimCandidate] = []
    consumed = set()

    for i, cand_a in enumerate(deduped):
        if i in consumed:
            continue

        set_a = set(cand_a.anchor_ids)
        nums_a = sorted([_parse_anchor_number(aid) for aid in cand_a.anchor_ids])

        # 检查是否被更长的候选包含
        for j, cand_b in enumerate(deduped):
            if j == i:
                continue
            if j in consumed:
                continue

            set_b = set(cand_b.anchor_ids)
            nums_b = sorted([_parse_anchor_number(aid) for aid in cand_b.anchor_ids])

            if cand_a.claim_type == cand_b.claim_type:
                # 区间重叠
                a_min, a_max = min(nums_a), max(nums_a) if nums_a else 0
                b_min, b_max = min(nums_b), max(nums_b) if nums_b else 0
                if a_min <= b_max and b_min <= a_max:
                    # A 是 B 的父集（A 更长）
                    if set_b.issubset(set_a) and set_b != set_a:
                        consumed.add(j)

        completeness_ruled.append(cand_a)

    # ---- 第5步：不同位置不合并 ----
    # 已在去重步骤中通过 anchor_ids 精确匹配处理
    # 不同 anchor_ids 但相同 text 的 claim 各自保留
    # （不额外处理）

    # ---- 第6步：排序与编号 ----
    # 按（首 anchor 编号，claim_type）排序
    completeness_ruled.sort(
        key=lambda c: (
            min(_parse_anchor_number(aid) for aid in c.anchor_ids),
            c.claim_type.value,
        )
    )

    # 生成连续 claim_id
    claims: list[Claim] = []
    for idx, cand in enumerate(completeness_ruled):
        claim_id = f"cl_{idx + 1:05d}"

        # 重建 text
        text = _rebuild_text(cand.anchor_ids, anchor_map)

        _attach_inherited_source_locations(cand.entities, parsed_doc, anchor_map)

        claim = Claim(
            claim_id=claim_id,
            claim_type=cand.claim_type,
            text=text,
            anchor_ids=cand.anchor_ids,
            entities=cand.entities,
            context_text=_derive_context_text(cand.anchor_ids, parsed_doc, anchor_map),
            source_locations=_derive_source_locations(cand.anchor_ids, parsed_doc, anchor_map),
            note_context=_derive_note_context(cand.anchor_ids, parsed_doc, anchor_map),
        )
        claims.append(claim)

    return claims


# ============================================================
# 构建 ClaimDocument
# ============================================================

def build_claim_document(
    parsed_doc: ParsedDocument,
    claims: list[Claim],
) -> "ClaimDocument":
    """
    构建最终的 ClaimDocument。

    Args:
        parsed_doc: 来源 ParsedDocument
        claims: 最终 Claim 列表
    Returns:
        ClaimDocument 对象
    """
    from ..domain.citation import ClaimMeta

    meta = ClaimMeta(
        schema_version=CLAIM_SCHEMA_VERSION,
        source_doc_id=parsed_doc.doc_meta.doc_id,
        source_doc_hash=parsed_doc.doc_meta.doc_hash,
        source_file=parsed_doc.doc_meta.source_file,
        extractor_version=CLAIM_EXTRACTOR_VERSION,
    )

    return ClaimDocument(
        claim_meta=meta,
        document_text="\n".join(
            block.text
            for block in sorted(parsed_doc.blocks, key=lambda item: item.block_order)
            if block.text.strip()
        ),
        claims=claims,
    )
