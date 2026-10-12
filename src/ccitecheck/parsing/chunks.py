"""
CCiteheck 文档分块构建器。

将 blocks 和 anchors 组装为语义上下文使用的 chunk。

规则：
  - 同一 section_path 下的连续 blocks 组成 chunk
  - chunk 不跨一级标题，尽量不跨二级标题
  - heading block 归入其后续内容所在的第一个 chunk
  - 每个 chunk 最多 1500 estimated tokens
  - 不使用逐句滑动窗口
  - 超长 block 按 sentence anchors 拆分，允许 1 句重叠
"""

from __future__ import annotations

from ..domain.document import Anchor, Block, Chunk, ParsedDocument
from .utils import estimate_tokens, make_id_counter


# ---- 常量 ----
# chunk 最大 token 数
TARGET_TOKEN_MAX = 1500


def build_chunks(parsed: ParsedDocument) -> ParsedDocument:
    """
    为 ParsedDocument 构建 chunks。

    会原地修改 parsed 对象的 chunks 字段。

    Args:
        parsed: 已填充 blocks 和 anchors 的 ParsedDocument

    Returns:
        同一个 ParsedDocument（chunks 已填充）
    """
    blocks = parsed.blocks
    anchors = parsed.anchors
    if not blocks:
        return parsed

    next_chunk_id = make_id_counter("c_", 5)

    # 构建 anchor 查找表：anchor_id → Anchor
    anchor_map: dict[str, Anchor] = {a.anchor: a for a in anchors}

    # 构建 block → 其 anchors 的映射
    block_anchors_map: dict[str, list[Anchor]] = {}
    for a in anchors:
        block_anchors_map.setdefault(a.block_id, []).append(a)

    # 按 block_order 排序 blocks（确保顺序）
    sorted_blocks = sorted(blocks, key=lambda b: b.block_order)

    chunks: list[Chunk] = []

    # 分组策略：
    # 1. 按 section_path 分组（相同 section_path 的连续 blocks 为一组）
    # 2. 每组内按 token 目标切分为多个 chunk

    # ---- 第一步：按 section_path 边界分组 ----
    # 同时在一级标题处切分
    groups: list[list[Block]] = []
    current_group: list[Block] = []

    for i, block in enumerate(sorted_blocks):
        # 判断是否需要开始新 group
        start_new = False

        if i > 0:
            prev_block = sorted_blocks[i - 1]
            # section_path 变化时切分
            if block.section_path != prev_block.section_path:
                start_new = True
            # 如果当前 block 是 heading，且与前一 block section_path 不同，切分

        if current_group and start_new:
            groups.append(current_group)
            current_group = []

        current_group.append(block)

    if current_group:
        groups.append(current_group)

    # ---- 第二步：在每个 group 内按 token 预算切分 ----
    for group in groups:
        group_chunks = _split_group_into_chunks(
            group, block_anchors_map, anchor_map, next_chunk_id
        )
        chunks.extend(group_chunks)

    parsed.chunks = chunks
    return parsed


def _split_group_into_chunks(
    group: list[Block],
    block_anchors_map: dict[str, list[Anchor]],
    anchor_map: dict[str, Anchor],
    next_chunk_id,
) -> list[Chunk]:
    """
    将一组 block 按 token 预算切分为多个 chunk。

    Args:
        group: 同 section_path 的连续 blocks
        block_anchors_map: block_id → 其 anchors 列表
        anchor_map: anchor_id → Anchor
        next_chunk_id: ID 生成器

    Returns:
        chunk 列表
    """
    chunks: list[Chunk] = []

    # 计算每个 block 的 token 估算
    block_tokens = {b.block_id: estimate_tokens(b.text) for b in group}

    # 贪心合并
    current_blocks: list[Block] = []
    current_tokens = 0

    for block in group:
        bt = block_tokens[block.block_id]

        # 如果当前 block 单独就超过上限，需要拆分
        if bt > TARGET_TOKEN_MAX:
            # 先 flush 当前累积的 chunk
            if current_blocks:
                chunks.append(_make_chunk(
                    current_blocks, block_anchors_map, next_chunk_id, []
                ))
                current_blocks = []
                current_tokens = 0

            # 超长 block 按 sentence anchors 拆分
            split_chunks = _split_long_block(
                block, block_anchors_map, anchor_map, next_chunk_id
            )
            chunks.extend(split_chunks)
            continue

        # 加入当前 block 后不超过上限时，继续合并到当前 chunk。
        if current_tokens + bt <= TARGET_TOKEN_MAX:
            current_blocks.append(block)
            current_tokens += bt
        else:
            # 当前 chunk 已满，先 flush
            if current_blocks:
                chunks.append(_make_chunk(
                    current_blocks, block_anchors_map, next_chunk_id, []
                ))
            # 开始新 chunk
            current_blocks = [block]
            current_tokens = bt

    # flush 剩余的
    if current_blocks:
        chunks.append(_make_chunk(
            current_blocks, block_anchors_map, next_chunk_id, []
        ))

    return chunks


def _split_long_block(
    block: Block,
    block_anchors_map: dict[str, list[Anchor]],
    anchor_map: dict[str, Anchor],
    next_chunk_id,
) -> list[Chunk]:
    """
    拆分超长 block 为多个 chunk。

    按 sentence anchors 切分，后一个 chunk 可与前一个重叠 1 个 anchor。
    重叠 anchor 记录在 overlap_anchor_ids 中。

    Args:
        block: 超长 block
        block_anchors_map: block_id → anchors
        anchor_map: anchor_id → Anchor
        next_chunk_id: ID 生成器

    Returns:
        多个 chunk 的列表
    """
    block_anchors = block_anchors_map.get(block.block_id, [])
    if not block_anchors:
        return []

    chunks: list[Chunk] = []
    current_anchors: list[Anchor] = []
    current_tokens = 0
    overlap_anchor_ids: list[str] = []

    # 最后一个被使用的 anchor ID（用于重叠）
    last_used_anchor_id: str | None = None

    for i, anchor in enumerate(block_anchors):
        at = estimate_tokens(anchor.text)

        # 如果加入当前 anchor 不会超上限
        if current_tokens + at <= TARGET_TOKEN_MAX:
            current_anchors.append(anchor)
            current_tokens += at
            last_used_anchor_id = anchor.anchor
        else:
            # flush 当前 chunk
            if current_anchors:
                chunks.append(_make_chunk(
                    [block], block_anchors_map, next_chunk_id, overlap_anchor_ids,
                    current_anchors,  # 指定的 anchors
                ))
                overlap_anchor_ids = [last_used_anchor_id] if last_used_anchor_id else []
                # 新 chunk 从前一个 anchor 开始（重叠一句）
                if last_used_anchor_id and last_used_anchor_id in anchor_map:
                    current_anchors = [anchor_map[last_used_anchor_id], anchor]
                    current_tokens = (
                        estimate_tokens(anchor_map[last_used_anchor_id].text) + at
                    )
                else:
                    current_anchors = [anchor]
                    current_tokens = at
            else:
                current_anchors = [anchor]
                current_tokens = at
            last_used_anchor_id = anchor.anchor

    # flush 最后的部分
    if current_anchors:
        chunks.append(_make_chunk(
            [block], block_anchors_map, next_chunk_id, overlap_anchor_ids,
            current_anchors,
        ))

    return chunks


def _make_chunk(
    blocks: list[Block],
    block_anchors_map: dict[str, list[Anchor]],
    next_chunk_id,
    overlap_anchor_ids: list[str] | None = None,
    specific_anchors: list[Anchor] | None = None,
) -> Chunk:
    """
    从一组 blocks 创建一个 chunk。

    Args:
        blocks: chunk 包含的 blocks
        block_anchors_map: block_id → anchors
        next_chunk_id: ID 生成器
        overlap_anchor_ids: 重叠 anchor 列表
        specific_anchors: 如果指定，则只使用这些 anchors（用于超长 block 拆分）

    Returns:
        Chunk 对象
    """
    if overlap_anchor_ids is None:
        overlap_anchor_ids = []

    # 收集所有 anchor
    if specific_anchors is not None:
        all_anchors = list(specific_anchors)
    else:
        all_anchors: list[Anchor] = []
        for b in blocks:
            bas = block_anchors_map.get(b.block_id, [])
            all_anchors.extend(bas)

    anchor_ids = [a.anchor for a in all_anchors]
    anchor_range = [anchor_ids[0], anchor_ids[-1]] if anchor_ids else ["", ""]
    block_ids = list(set(b.block_id for b in blocks))
    # 保持 block_order 顺序
    block_ids.sort(key=lambda bid: next(
        (b.block_order for b in blocks if b.block_id == bid), 0
    ))

    # 计算 estimated_tokens
    total_tokens = sum(estimate_tokens(a.text) for a in all_anchors)

    # section_path：使用第一个 block 的
    section_path = blocks[0].section_path if blocks else []

    return Chunk(
        chunk_id=next_chunk_id(),
        section_path=section_path,
        block_ids=block_ids,
        anchor_ids=anchor_ids,
        anchor_range=anchor_range,
        estimated_tokens=total_tokens,
        overlap_anchor_ids=overlap_anchor_ids,
    )
