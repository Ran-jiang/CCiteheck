"""
CCiteheck 文档领域模型。

本模块使用 Pydantic v2 定义 DOCX 解析产物的所有数据结构。
三层架构：
  - Anchor Layer：句级锚点层（细粒度）
  - Block Layer：文档结构块层（中粒度）
  - Chunk Layer：语义上下文层（粗粒度）
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


# ---- 辅助类型 ----

class BlockType(str, Enum):
    """文档结构块类型"""
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE_CELL = "table_cell"
    FOOTNOTE = "footnote"
    ENDNOTE = "endnote"


class HeadingSource(str, Enum):
    """标题识别来源"""
    STYLE = "style"
    PATTERN = "pattern"


class BlockRelationType(str, Enum):
    """可由文档结构确定的 block 关系。"""
    PREVIOUS_BLOCK = "previous_block"
    LIST_LEAD = "list_lead"
    TABLE_LEFT = "table_left"
    TABLE_ABOVE = "table_above"
    NOTE_REFERENCE = "note_reference"


class BlockRelation(BaseModel):
    """从当前 block 指向上下文 block 的显式关系。"""
    relation_type: BlockRelationType
    target_block_id: str


class NoteReference(BaseModel):
    """正文中指向脚注或尾注的显式引用点。"""

    note_type: Literal["footnote", "endnote"]
    note_id: str
    char_offset: int = Field(
        ge=0,
        description="引用标记在归一化 block.text 中对应的插入位置",
    )


# ---- Anchor Layer ----

class Anchor(BaseModel):
    """
    句级锚点。

    每个句子生成一个 anchor。anchor 是逻辑坐标，
    通过 block_id + char_start + char_end 定位到 block.text 中的具体文本。
    """
    anchor: str = Field(description="锚点编号，格式 line00001")
    text: str = Field(description="句子文本，从所属 block.text 派生")
    block_id: str = Field(description="所属 block 的 ID")
    # para_index 仅对段落类 block（heading/paragraph/list_item）有意义
    para_index: Optional[int] = Field(default=None, description="原始段落序号，从0开始")
    note_type: Optional[str] = Field(default=None, description="footnote/endnote；正文为 null")
    note_id: Optional[str] = Field(default=None, description="Word 注释 ID；正文为 null")
    char_start: int = Field(description="在 block.text 中的起始偏移（左闭）", ge=0)
    char_end: int = Field(description="在 block.text 中的结束偏移（右开）", ge=0)


# ---- Block Layer ----

class Block(BaseModel):
    """
    文档结构块。

    block 是文档的结构单元。block.text 是解析后、归一化后的规范文本来源。
    所有偏移量都基于 block.text 计算。
    """
    block_id: str = Field(description="block 唯一 ID，格式 b_00001")
    type: BlockType = Field(description="block 类型")
    text: str = Field(description="归一化后的规范文本")
    style: Optional[str] = Field(default=None, description="DOCX 段落样式名")
    # ---- 结构路径 ----
    section_path: list[str] = Field(default_factory=list, description="从一级标题到当前标题的路径")
    # ---- 顺序索引 ----
    body_order: int = Field(description="DOCX body 顶层元素顺序，从0开始")
    block_order: int = Field(description="所有 block 的全局阅读顺序，从0开始")
    para_index: Optional[int] = Field(default=None, description="原始段落序号，table_cell 时为 null")
    note_type: Optional[str] = Field(default=None, description="footnote/endnote；正文为 null")
    note_id: Optional[str] = Field(default=None, description="Word 注释 ID；正文为 null")
    note_references: list[NoteReference] = Field(
        default_factory=list,
        description="正文块中出现的脚注/尾注引用点",
    )
    # ---- 表格定位 ----
    table_index: Optional[int] = Field(default=None, description="表格编号，从0开始")
    row_index: Optional[int] = Field(default=None, description="行编号，从0开始")
    cell_index: Optional[int] = Field(default=None, description="单元格编号，从0开始")
    row_start: Optional[int] = Field(default=None, description="合并单元格覆盖的起始行（含）")
    row_end: Optional[int] = Field(default=None, description="合并单元格覆盖的结束行（含）")
    col_start: Optional[int] = Field(default=None, description="合并单元格覆盖的起始列（含）")
    col_end: Optional[int] = Field(default=None, description="合并单元格覆盖的结束列（含）")
    row_span: int = Field(default=1, ge=1, description="纵向合并跨度")
    col_span: int = Field(default=1, ge=1, description="横向合并跨度")
    # ---- 自动编号 ----
    has_numbering: bool = Field(default=False, description="段落是否携带 Word 自动编号")
    numbering_text: Optional[str] = Field(default=None, description="从 numbering.xml 还原的编号文本")
    numbering_unresolved: bool = Field(default=False, description="无法可靠还原编号文本")
    # ---- 列举项 ----
    is_list_item: bool = Field(default=False, description="是否为列举项")
    list_group_id: Optional[str] = Field(default=None, description="列举组 ID，格式 lg_00001")
    # ---- 法条标志 ----
    is_article_start: bool = Field(default=False, description="是否为'第X条'起始")
    heading_source: Optional[HeadingSource] = Field(default=None, description="标题识别来源")
    # ---- 锚点关联 ----
    anchor_range: list[str] = Field(default_factory=list, description="[第一个anchor, 最后一个anchor]")
    sentence_anchors: list[str] = Field(default_factory=list, description="该 block 内所有 anchor 编号列表")
    # 外部平台坐标只用于定位原文，不参与解析和判定。
    external_block_id: Optional[str] = Field(default=None, description="来源平台的稳定块 ID")
    external_parent_id: Optional[str] = Field(default=None, description="来源平台的父块 ID")
    source_metadata: dict[str, Any] = Field(default_factory=dict, description="来源平台附加信息")
    relations: list[BlockRelation] = Field(default_factory=list, description="确定性 block 上下文关系")

    @model_validator(mode="after")
    def normalize_table_span(self) -> "Block":
        """普通单元格的范围默认为其逻辑行列坐标。"""
        if self.type != BlockType.TABLE_CELL:
            return self
        if self.row_start is None:
            self.row_start = self.row_index
        if self.row_end is None:
            self.row_end = self.row_index
        if self.col_start is None:
            self.col_start = self.cell_index
        if self.col_end is None:
            self.col_end = self.cell_index
        if self.row_start is not None and self.row_end is not None:
            self.row_span = self.row_end - self.row_start + 1
        if self.col_start is not None and self.col_end is not None:
            self.col_span = self.col_end - self.col_start + 1
        return self


# ---- Chunk Layer ----

class Chunk(BaseModel):
    """
    语义上下文打包单元。

    chunk 不重复存正文文本，使用时通过 anchor_ids 动态渲染。
    """
    chunk_id: str = Field(description="chunk 唯一 ID，格式 c_00001")
    section_path: list[str] = Field(default_factory=list, description="chunk 所属的标题路径")
    block_ids: list[str] = Field(default_factory=list, description="chunk 覆盖的 block ID 列表")
    anchor_ids: list[str] = Field(default_factory=list, description="chunk 实际包含的 anchor 编号列表")
    anchor_range: list[str] = Field(default_factory=list, description="[第一个anchor, 最后一个anchor]")
    estimated_tokens: int = Field(default=0, description="粗略 token 估算")
    overlap_anchor_ids: list[str] = Field(default_factory=list, description="重叠 anchor 列表（仅超长 block 拆分时）")


# ---- 文档元信息 ----

class DocMeta(BaseModel):
    """文档级元信息"""
    schema_version: str = Field(default="0.1", description="schema 版本号")
    doc_id: str = Field(default_factory=lambda: str(uuid4()), description="文档唯一 ID（uuid4）")
    source_file: str = Field(default="", description="原始 DOCX 文件名")
    doc_hash: str = Field(default="", description="原始文件 SHA-256 摘要")
    source_platform: Literal["docx"] = Field(default="docx", description="Word DOCX 来源")
    source_document_id: Optional[str] = Field(default=None, description="来源平台文档 ID")
    source_revision: Optional[str] = Field(default=None, description="来源平台文档版本")
    parsed_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="解析时间（ISO-8601）"
    )


# ---- 顶层文档 ----

class ParsedDocument(BaseModel):
    """
    CCiteheck 解析产出的顶层结构。

    每次解析视为一次文档快照（snapshot）。
    """
    doc_meta: DocMeta = Field(default_factory=DocMeta)
    blocks: list[Block] = Field(default_factory=list)
    anchors: list[Anchor] = Field(default_factory=list)
    chunks: list[Chunk] = Field(default_factory=list)
