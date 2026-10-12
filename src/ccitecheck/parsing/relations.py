"""为已解析文档构建平台无关的 Block 关系。"""

from __future__ import annotations

from ..domain.document import (
    Block,
    BlockRelation,
    BlockRelationType,
    BlockType,
    ParsedDocument,
)


def build_block_relations(parsed: ParsedDocument) -> ParsedDocument:
    """只根据结构坐标建关系，不在解析层识别法源语义。"""
    for block in parsed.blocks:
        block.relations = []

    ordered = sorted(parsed.blocks, key=lambda item: item.block_order)
    previous: Block | None = None
    list_leads: dict[str, Block] = {}
    for block in ordered:
        if block.list_group_id and not block.is_list_item:
            list_leads[block.list_group_id] = block
        elif block.list_group_id and block.list_group_id in list_leads:
            _add(block, BlockRelationType.LIST_LEAD, list_leads[block.list_group_id])

        if previous and _are_adjacent_body_blocks(previous, block):
            _add(block, BlockRelationType.PREVIOUS_BLOCK, previous)
        previous = block

    tables: dict[int, list[Block]] = {}
    for block in ordered:
        if block.type == BlockType.TABLE_CELL and block.table_index is not None:
            tables.setdefault(block.table_index, []).append(block)
    for cells in tables.values():
        for current in cells:
            left = _nearest_left_cell(current, cells)
            if left:
                _add(current, BlockRelationType.TABLE_LEFT, left)
            above = _nearest_above_cell(current, cells)
            if above:
                _add(current, BlockRelationType.TABLE_ABOVE, above)

    note_blocks: dict[tuple[str, str], Block] = {}
    for block in ordered:
        if block.note_type and block.note_id:
            note_blocks.setdefault((block.note_type, block.note_id), block)
    for block in ordered:
        for reference in block.note_references:
            target = note_blocks.get((reference.note_type, reference.note_id))
            if target is not None:
                _add(block, BlockRelationType.NOTE_REFERENCE, target)
    return parsed


def _add(block: Block, kind: BlockRelationType, target: Block) -> None:
    relation = BlockRelation(relation_type=kind, target_block_id=target.block_id)
    if relation not in block.relations:
        block.relations.append(relation)


def _are_adjacent_body_blocks(previous: Block, current: Block) -> bool:
    if previous.type == BlockType.TABLE_CELL or current.type == BlockType.TABLE_CELL:
        return False
    if previous.note_type != current.note_type or previous.section_path != current.section_path:
        return False
    if previous.body_order + 1 != current.body_order:
        return False
    if previous.para_index is not None and current.para_index is not None:
        return previous.para_index + 1 == current.para_index
    return True


def _bounds(block: Block) -> tuple[int, int, int, int]:
    row = block.row_index or 0
    col = block.cell_index or 0
    return (
        block.row_start if block.row_start is not None else row,
        block.row_end if block.row_end is not None else row,
        block.col_start if block.col_start is not None else col,
        block.col_end if block.col_end is not None else col,
    )


def _nearest_left_cell(current: Block, cells: list[Block]) -> Block | None:
    row_start, row_end, col_start, _ = _bounds(current)
    candidates = []
    for candidate in cells:
        if candidate is current:
            continue
        c_row_start, c_row_end, _, c_col_end = _bounds(candidate)
        if c_row_start <= row_start and c_row_end >= row_end and c_col_end < col_start:
            candidates.append(candidate)
    return max(candidates, key=lambda item: _bounds(item)[3], default=None)


def _nearest_above_cell(current: Block, cells: list[Block]) -> Block | None:
    row_start, _, col_start, col_end = _bounds(current)
    candidates = []
    for candidate in cells:
        if candidate is current:
            continue
        _, c_row_end, c_col_start, c_col_end = _bounds(candidate)
        columns_overlap = c_col_start <= col_start and c_col_end >= col_end
        if columns_overlap and c_row_end < row_start:
            candidates.append(candidate)
    return max(candidates, key=lambda item: _bounds(item)[1], default=None)


__all__ = ["build_block_relations"]
