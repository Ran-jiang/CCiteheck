"""
CCiteheck 文档结构不变量校验。

实现 ParsedDocument 的全量不变量校验。
校验失败返回违反项列表（字符串），通过返回空列表。
"""

from __future__ import annotations

from ..domain.document import Anchor, Block, ParsedDocument


def validate_parsed_document(doc: ParsedDocument) -> list[str]:
    """
    对 ParsedDocument 执行所有不变量校验。

    Args:
        doc: 已解析的文档

    Returns:
        违反项字符串列表。空列表表示全部校验通过。
    """
    violations: list[str] = []

    # 构建查找表
    anchor_map: dict[str, Anchor] = {a.anchor: a for a in doc.anchors}
    block_map: dict[str, Block] = {b.block_id: b for b in doc.blocks}

    # ---- 1. 无损分句 ----
    violations.extend(_check_lossless_split(doc, anchor_map, block_map))

    # ---- 2. 锚点顺序 ----
    violations.extend(_check_anchor_order(doc, block_map))

    # ---- 3. 归属唯一 ----
    violations.extend(_check_anchor_ownership(doc, anchor_map, block_map))

    # ---- 4. chunk 覆盖 ----
    violations.extend(_check_chunk_coverage(doc, anchor_map))

    # ---- 5. chunk 连续性 ----
    violations.extend(_check_chunk_continuity(doc, block_map))

    # ---- 6. ID 唯一性 ----
    violations.extend(_check_id_uniqueness(doc))

    # ---- 7. anchor_range 自洽 ----
    violations.extend(_check_anchor_range_consistency(doc, anchor_map, block_map))

    # ---- 8. Block 关系与表格合并范围 ----
    violations.extend(_check_block_structure(doc, block_map))

    return violations


def _check_block_structure(
    doc: ParsedDocument,
    block_map: dict[str, Block],
) -> list[str]:
    violations: list[str] = []
    for block in doc.blocks:
        for relation in block.relations:
            if relation.target_block_id not in block_map:
                violations.append(
                    f"[block_relation] block {block.block_id}: "
                    f"target {relation.target_block_id} 不存在"
                )
        coordinates = (block.row_start, block.row_end, block.col_start, block.col_end)
        if block.type.value == "table_cell":
            if any(item is None for item in coordinates):
                violations.append(f"[table_span] block {block.block_id}: 合并范围不完整")
                continue
            if block.row_end < block.row_start or block.col_end < block.col_start:
                violations.append(f"[table_span] block {block.block_id}: 合并范围倒置")
            elif (
                block.row_span != block.row_end - block.row_start + 1
                or block.col_span != block.col_end - block.col_start + 1
            ):
                violations.append(f"[table_span] block {block.block_id}: span 与范围不一致")
    return violations


def _check_lossless_split(
    doc: ParsedDocument,
    anchor_map: dict[str, Anchor],
    block_map: dict[str, Block],
) -> list[str]:
    """校验无损分句不变量。"""
    violations: list[str] = []

    for block in doc.blocks:
        # 获取该 block 的所有 anchors
        block_anchors = _get_block_anchors(block, doc.anchors)

        # 校验 join 后等于 block.text
        joined = "".join(a.text for a in block_anchors)
        if joined != block.text:
            violations.append(
                f"[无损分句] block {block.block_id}: "
                f"join(anchor.texts) != block.text. "
                f"joined={repr(joined)[:80]}, expected={repr(block.text)[:80]}"
            )

        # 校验每个 anchor 的偏移
        for anchor in block_anchors:
            extracted = block.text[anchor.char_start:anchor.char_end]
            if extracted != anchor.text:
                violations.append(
                    f"[无损分句] anchor {anchor.anchor} in block {block.block_id}: "
                    f"block.text[{anchor.char_start}:{anchor.char_end}]='{extracted}' "
                    f"!= anchor.text='{anchor.text}'"
                )

    return violations


def _check_anchor_order(
    doc: ParsedDocument,
    block_map: dict[str, Block],
) -> list[str]:
    """校验锚点顺序不变量。"""
    violations: list[str] = []

    if not doc.anchors:
        return violations

    # 检查 names 是否严格升序
    for i in range(1, len(doc.anchors)):
        prev = doc.anchors[i - 1]
        curr = doc.anchors[i]

        # 名称顺序（line00001 < line00002）
        if prev.anchor >= curr.anchor:
            violations.append(
                f"[锚点顺序] anchor 编号顺序错误: "
                f"{prev.anchor} 在 {curr.anchor} 之前但编号不小于"
            )

        # 阅读顺序：(block_order, char_start)
        prev_block = block_map.get(prev.block_id)
        curr_block = block_map.get(curr.block_id)
        if prev_block and curr_block:
            if (prev_block.block_order, prev.char_start) > (curr_block.block_order, curr.char_start):
                violations.append(
                    f"[锚点顺序] 阅读顺序错误: "
                    f"{prev.anchor} (block_order={prev_block.block_order}, char_start={prev.char_start}) "
                    f"应排在 {curr.anchor} (block_order={curr_block.block_order}, char_start={curr.char_start}) 之后"
                )

    return violations


def _check_anchor_ownership(
    doc: ParsedDocument,
    anchor_map: dict[str, Anchor],
    block_map: dict[str, Block],
) -> list[str]:
    """校验归属唯一不变量。"""
    violations: list[str] = []

    # 每个 anchor 只能属于一个 block（由 anchor.block_id 保证）
    # 反向校验：block.sentence_anchors 与 anchors 表一致
    for block in doc.blocks:
        expected_anchors = _get_block_anchors(block, doc.anchors)
        expected_ids = [a.anchor for a in expected_anchors]

        if block.sentence_anchors != expected_ids:
            violations.append(
                f"[归属唯一] block {block.block_id}: "
                f"sentence_anchors={block.sentence_anchors} "
                f"!= expected={expected_ids}"
            )

    # 确保没有 anchor 在多个 block 中出现
    seen_anchor_ids: set[str] = set()
    for block in doc.blocks:
        for aid in block.sentence_anchors:
            if aid in seen_anchor_ids:
                violations.append(
                    f"[归属唯一] anchor {aid} 在多个 block 的 sentence_anchors 中出现"
                )
            seen_anchor_ids.add(aid)

    return violations


def _check_chunk_coverage(
    doc: ParsedDocument,
    anchor_map: dict[str, Anchor],
) -> list[str]:
    """校验 chunk 覆盖不变量。"""
    violations: list[str] = []

    # 收集每个 anchor 出现在哪些 chunk 中
    anchor_to_chunks: dict[str, list[str]] = {}
    for chunk in doc.chunks:
        for aid in chunk.anchor_ids:
            anchor_to_chunks.setdefault(aid, []).append(chunk.chunk_id)

    # 收集所有 overlap_anchor_ids
    all_overlap_ids: set[str] = set()
    for chunk in doc.chunks:
        for aid in chunk.overlap_anchor_ids:
            all_overlap_ids.add(aid)

    # 每个 anchor 至少出现在一个 chunk 中
    for anchor in doc.anchors:
        if anchor.anchor not in anchor_to_chunks:
            violations.append(
                f"[chunk覆盖] anchor {anchor.anchor} 未出现在任何 chunk 中"
            )

    # 如果出现在多个 chunk 中，必须在 overlap_anchor_ids 中
    for aid, chunk_ids in anchor_to_chunks.items():
        if len(chunk_ids) > 1:
            if aid not in all_overlap_ids:
                violations.append(
                    f"[chunk覆盖] anchor {aid} 出现在 {len(chunk_ids)} 个 chunk "
                    f"({chunk_ids}) 但不在任何 overlap_anchor_ids 中"
                )

    return violations


def _check_chunk_continuity(
    doc: ParsedDocument,
    block_map: dict[str, Block],
) -> list[str]:
    """校验 chunk 连续性不变量。"""
    violations: list[str] = []

    for chunk in doc.chunks:
        if not chunk.block_ids:
            continue

        # 获取各 block 的 block_order
        orders: list[int] = []
        for bid in chunk.block_ids:
            block = block_map.get(bid)
            if block:
                orders.append(block.block_order)

        if not orders:
            continue

        orders.sort()

        # 同一 chunk 的 block 必须在阅读顺序中连续；重复仅用于超长块重叠。
        # 由于超长 block 拆分场景，同一个 block_id 可以出现在多个 chunk
        # 我们只检查顺序是否单调递增
        for i in range(1, len(orders)):
            if orders[i] != orders[i - 1] + 1 and orders[i] != orders[i - 1]:
                violations.append(
                    f"[chunk连续性] chunk {chunk.chunk_id}: "
                    f"block_order {orders[i - 1]} 与 {orders[i]} 不连续"
                )

        # 检查单调性
        prev_order = -1
        for bid in chunk.block_ids:
            block = block_map.get(bid)
            if block:
                if block.block_order < prev_order:
                    violations.append(
                        f"[chunk连续性] chunk {chunk.chunk_id}: "
                        f"block_ids 对应的 block_order 不单调递增"
                    )
                    break
                prev_order = block.block_order

    return violations


def _check_id_uniqueness(doc: ParsedDocument) -> list[str]:
    """校验 ID 唯一性。"""
    violations: list[str] = []

    # block_id 唯一
    block_ids: list[str] = [b.block_id for b in doc.blocks]
    if len(block_ids) != len(set(block_ids)):
        violations.append("[ID唯一性] block_id 重复")

    # anchor 唯一
    anchor_ids: list[str] = [a.anchor for a in doc.anchors]
    if len(anchor_ids) != len(set(anchor_ids)):
        violations.append("[ID唯一性] anchor 重复")

    # chunk_id 唯一
    chunk_ids: list[str] = [c.chunk_id for c in doc.chunks]
    if len(chunk_ids) != len(set(chunk_ids)):
        violations.append("[ID唯一性] chunk_id 重复")

    return violations


def _check_anchor_range_consistency(
    doc: ParsedDocument,
    anchor_map: dict[str, Anchor],
    block_map: dict[str, Block],
) -> list[str]:
    """校验 anchor_range 自洽。"""
    violations: list[str] = []

    # block 级别
    for block in doc.blocks:
        if not block.sentence_anchors:
            if block.anchor_range:
                violations.append(
                    f"[anchor_range] block {block.block_id}: "
                    f"sentence_anchors 为空但 anchor_range={block.anchor_range}"
                )
            continue

        expected_range = [block.sentence_anchors[0], block.sentence_anchors[-1]]
        if block.anchor_range != expected_range:
            violations.append(
                f"[anchor_range] block {block.block_id}: "
                f"anchor_range={block.anchor_range} != expected={expected_range}"
            )

    # chunk 级别
    for chunk in doc.chunks:
        if not chunk.anchor_ids:
            if chunk.anchor_range and chunk.anchor_range != ["", ""]:
                violations.append(
                    f"[anchor_range] chunk {chunk.chunk_id}: "
                    f"anchor_ids 为空但 anchor_range={chunk.anchor_range}"
                )
            continue

        expected_range = [chunk.anchor_ids[0], chunk.anchor_ids[-1]]
        if chunk.anchor_range != expected_range:
            violations.append(
                f"[anchor_range] chunk {chunk.chunk_id}: "
                f"anchor_range={chunk.anchor_range} != expected={expected_range}"
            )

    return violations


def _get_block_anchors(block: Block, all_anchors: list[Anchor]) -> list[Anchor]:
    """
    获取属于指定 block 的 anchors，按 char_start 排序。

    Args:
        block: Block 对象
        all_anchors: 全局 anchors 列表

    Returns:
        排序后的 Anchor 列表
    """
    block_anchors = [a for a in all_anchors if a.block_id == block.block_id]
    block_anchors.sort(key=lambda a: a.char_start)
    return block_anchors
