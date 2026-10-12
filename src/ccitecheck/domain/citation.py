"""
CCiteheck 引用识别领域模型。

本模块使用 Pydantic v2 定义可验证主张识别层的所有数据结构。

核心设计决策（代码层面体现）：
  - claim.text 由系统从 anchors 重建，不由抽取器提供
    → ClaimCandidate 不含 text，由 arbiter 构建 Claim 时填入重建文本
  - entities 按 claim_type 使用 pydantic 子模型，不用 dict[str, Any]
    → 序列化后仍是 JSON object，同时获得类型安全和校验
  - ClaimCandidate 是内部中间类型，不进入最终 JSON
    → 抽取器产出 Candidate，必须经 Arbiter 裁决后才成为 Claim
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

CLAIM_SCHEMA_VERSION = "0.6"
CLAIM_EXTRACTOR_VERSION = "0.5"


# ============================================================
# 枚举定义
# ============================================================

class ClaimType(str, Enum):
    """
    可验证主张类型。

    legal_source_claim: 含明确法律规范引用的完整主张
        - 句子中有《XX法》第X条等规范引用
        - 即使后半句包含法律判断（如"被告应当承担违约责任"），也抽取为此类型
        - 理由：后续至少需要检索该法条并返回原文

        - 法条号后紧跟"规定/明确/指出/载明"等触发词
        - 触发词后有实体转述内容

    case_citation: 案例引用
        - 有明确案号（如（2021）最高法民申1234号）
        - 或明确可检索线索（指导案例第X号、公报案例、X诉Y案等）

    case_holding_paraphrase: 带明确案例依据的裁判观点转述
        - 同一句中既有案例引用，又有"认为/指出/裁判要旨"等观点触发词
    """
    LEGAL_SOURCE_CLAIM = "legal_source_claim"
    CASE_CITATION = "case_citation"
    CASE_HOLDING_PARAPHRASE = "case_holding_paraphrase"


# ============================================================
# 实体子模型（按 claim_type 分别定义）
# ============================================================

class ArticleRef(BaseModel):
    """
    条款引用。

    article 保留"之一"等后缀，如"第一百八十四条之一"。
    条款号格式由规则抽取器保证。
    """
    article: str = Field(description="条款文本，如'第四十八条'、'第一百八十四条之一'")
    raw_locator: str | None = Field(
        default=None,
        description="原文误用款作条号时保留原始定位文本",
    )
    paragraphs: list[str] = Field(
        default_factory=list,
        description="款号列表，如['第一款', '第二款']"
    )
    items: list[str] = Field(
        default_factory=list,
        description="项号列表，如['第（一）项']"
    )
    model_config = ConfigDict(extra="forbid")


class StructureUnit(BaseModel):
    """章节链中的一级，如（编, 3）。"""
    unit: str = Field(description="编/分编/章/节")
    number: Optional[int] = Field(default=None, description="序号；无号节点为 None")
    number_text: str = Field(description="原文标签，如'第三编'")
    model_config = ConfigDict(extra="forbid")


class StructureRef(BaseModel):
    """章节引用，如《民法典》第三编第四章（无条号）。"""
    label: str = Field(description="原文章节标签连写，如'第三编第四章'")
    units: list[StructureUnit] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


class InheritedSourceReference(BaseModel):
    anchor_id: str | None = None
    source_location: Optional["SourceLocation"] = None
    model_config = ConfigDict(extra="forbid")


class LegalSourceRecognition(BaseModel):
    form: Literal["explicit", "bare", "inherited"] = "explicit"
    mention_span: tuple[int, int] | None = None
    inherited_from: InheritedSourceReference | None = None
    resolver: Literal["direct", "structure", "lexicon", "context"] = Field(
        default="direct",
        description="法名由原文、词典或承前上下文确认",
    )
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_method(self):
        expected = {
            "explicit": "direct",
            "bare": "structure",
            "inherited": "context",
        }[self.form]
        if self.resolver != expected and not (
            self.form == "bare" and self.resolver == "lexicon"
        ):
            raise ValueError(
                f"recognition.form={self.form} 必须使用 resolver={expected}"
            )
        if self.form != "inherited" and self.inherited_from is not None:
            raise ValueError("只有 inherited 法名可以设置 inherited_from")
        return self


class LegalSource(BaseModel):
    """
    法律规范来源。

    title 为书名号内文本，不含书名号本身。
    recognition 分层记录法名的文本形式、原文范围和承前来源。
    """
    title: str = Field(description="法规名称，不含书名号")
    canonical_title: Optional[str] = Field(
        default=None,
        description="确定性名称形态（不表示法规已被检索确认存在）",
    )
    raw_title_candidate: Optional[str] = Field(
        default=None,
        description="裸引用中仅凭文本边界得到的法名候选",
    )
    raw_time: Optional[str] = Field(
        default=None,
        description="原文中与该来源相邻的时间或相对版本表达，不作解释",
    )
    jurisdiction: Optional[str] = Field(
        default=None,
        description="旧版输入兼容字段；法域由 Query Construction 生成",
    )
    articles: list[ArticleRef] = Field(
        default_factory=list,
        description="条款引用列表"
    )
    structures: list[StructureRef] = Field(
        default_factory=list,
        description="章节引用列表（如第三编第四章；仅在无条款引用时抽取）"
    )
    recognition: LegalSourceRecognition = Field(default_factory=LegalSourceRecognition)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_reference_shape(self):
        if self.articles and self.structures:
            raise ValueError("articles 与 structures 不能同时存在")
        return self


class UnresolvedLegalMention(BaseModel):
    """已识别为法规引用结构、但尚未确认具体法规身份的原文候选。"""

    raw_text: str
    articles: list[ArticleRef] = Field(default_factory=list)
    reason: Literal["law_identity_unresolved"] = "law_identity_unresolved"
    resolution_anchor_span: tuple[int, int] | None = None
    model_config = ConfigDict(extra="forbid")


class CitationLocator(BaseModel):
    article: str
    paragraph: str | None = None
    item: str | None = None
    model_config = ConfigDict(extra="forbid")


class CitationOccurrence(BaseModel):
    """原文中一次独立出现的法规引用。"""

    law_title: str
    mention_id: str | None = None
    raw_law_title: str | None = None
    locator: CitationLocator
    role: Literal["direct", "nested", "carry_forward"] = "direct"
    citation_span: tuple[int, int] | None = None
    span_status: Literal["located", "fallback", "error"] = "fallback"
    model_config = ConfigDict(extra="forbid")


class CaseRef(BaseModel):
    """
    案例引用。

    有案号时填写 case_number；无案号但可检索时填写 case_name。
    """
    case_number: Optional[str] = Field(
        default=None,
        description="案号，如'（2021）最高法民申1234号'"
    )
    case_name: Optional[str] = Field(
        default=None,
        description="案例名称"
    )
    court: Optional[str] = Field(
        default=None,
        description="法院名称"
    )
    document_type: Optional[str] = Field(
        default=None,
        description="文中写明的文书类型，如'判决书'、'裁定书'；未写明为 None"
    )
    jurisdiction: str = Field(
        default="CN",
        description="法域：CN（中国）或 FOREIGN（外国判例，超出核查边界）"
    )
    model_config = ConfigDict(extra="forbid")


class LegalSourceClaimEntities(BaseModel):
    """
    legal_source_claim 的实体。

    支持多法源（设计决策2.4）：
    一句引用多个法律规范时，全部列入 legal_sources，不拆分。
    """
    legal_sources: list[LegalSource] = Field(
        default_factory=list,
        description="法律规范来源列表"
    )
    unresolved_legal_mentions: list[UnresolvedLegalMention] = Field(
        default_factory=list,
        description="结构已识别但法规身份尚未确认的候选",
    )
    citations: list[CitationOccurrence] = Field(
        default_factory=list,
        description="按原文出现次数保存的法规引用；检索层再按 locator 去重",
    )
    alias_declarations: list["AliasDeclaration"] = Field(default_factory=list)
    structural_relations: list["NestedRelationCandidate"] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


class AliasDeclaration(BaseModel):
    full_name_raw: str
    alias_raw: str
    declaration_span: tuple[int, int] | None = None
    model_config = ConfigDict(extra="forbid")


class NestedRelationCandidate(BaseModel):
    parent_mention_id: str
    child_mention_id: str
    basis: Literal["text_scope"] = "text_scope"
    model_config = ConfigDict(extra="forbid")


class CaseCitationEntities(BaseModel):
    """案例引用的实体。"""
    case_refs: list[CaseRef] = Field(
        default_factory=list,
        description="案例引用列表"
    )
    model_config = ConfigDict(extra="forbid")


class CaseHoldingParaphraseEntities(BaseModel):
    """
    case_holding_paraphrase 的实体。

    没有明确 case_ref 时绝不抽取此类型——即使出现"法院认为""本院认为"。
    """
    case_refs: list[CaseRef] = Field(
        default_factory=list,
        description="案例引用列表（观点转述通常长度为1）"
    )
    model_config = ConfigDict(extra="forbid")


ClaimEntities = Union[
    LegalSourceClaimEntities,
    CaseCitationEntities,
    CaseHoldingParaphraseEntities,
]

_ENTITY_MODEL_BY_CLAIM_TYPE = {
    ClaimType.LEGAL_SOURCE_CLAIM: LegalSourceClaimEntities,
    ClaimType.CASE_CITATION: CaseCitationEntities,
    ClaimType.CASE_HOLDING_PARAPHRASE: CaseHoldingParaphraseEntities,
}


def _coerce_entities(data):
    """按 claim_type 还原实体子模型，保证 JSON 可以无损往返。"""
    if not isinstance(data, dict):
        return data
    raw_entities = data.get("entities")
    if raw_entities is None or isinstance(
        raw_entities,
        (LegalSourceClaimEntities, CaseCitationEntities, CaseHoldingParaphraseEntities),
    ):
        return data
    claim_type = data.get("claim_type")
    try:
        normalized_claim_type = ClaimType(claim_type)
    except (TypeError, ValueError):
        normalized_claim_type = None
    model = _ENTITY_MODEL_BY_CLAIM_TYPE.get(normalized_claim_type)
    if model is not None:
        data = dict(data)
        data["entities"] = model.model_validate(raw_entities)
    return data


# ============================================================
# 内部中间类型：ClaimCandidate（不进入最终 JSON）
# ============================================================

class SourceLocation(BaseModel):
    """Word 原文定位坐标。"""

    platform: Literal["docx"] = "docx"
    document_id: Optional[str] = None
    revision: Optional[str] = None
    block_id: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    anchor_text: str = Field(default="", description="用于来源平台按文本自愈定位的 Anchor 原文")
    occurrence: Optional[int] = Field(
        default=None,
        ge=0,
        description="anchor_text 在所属块内从零开始的出现序号，用于多命中消歧",
    )
    table_index: Optional[int] = None
    row_index: Optional[int] = None
    cell_index: Optional[int] = None
    row_start: Optional[int] = None
    row_end: Optional[int] = None
    col_start: Optional[int] = None
    col_end: Optional[int] = None
    model_config = ConfigDict(extra="forbid")


class NoteContext(BaseModel):
    """脚注/尾注核验结果与正文引用点之间的稳定关系。"""

    note_type: Literal["footnote", "endnote"]
    note_id: str
    referenced_from: list[SourceLocation] = Field(default_factory=list)
    reference_anchor_id: str | None = None
    model_config = ConfigDict(extra="forbid")


class ClaimCandidate(BaseModel):
    """
    抽取器产出的中间候选。

    这是内部类型，不直接进入最终 ClaimDocument JSON。
    所有候选必须经过 Claim Arbiter 裁决后才成为 Claim。

    抽取器只负责回答"哪些 anchor 构成一个 claim、是什么类型、含哪些实体"。
    claim.text 由 arbiter 从 anchor 重建，不由抽取器提供。
    """
    claim_type: ClaimType = Field(description="主张类型")
    anchor_ids: list[str] = Field(description="anchor 编号列表")
    entities: ClaimEntities = Field(description="实体信息（各 claim_type 的对应子模型）")

    @model_validator(mode="before")
    @classmethod
    def restore_entity_type(cls, data):
        return _coerce_entities(data)

    @model_validator(mode="after")
    def validate_entity_type(self):
        expected = _ENTITY_MODEL_BY_CLAIM_TYPE[self.claim_type]
        if not isinstance(self.entities, expected):
            raise ValueError(
                f"{self.claim_type.value} 必须使用 {expected.__name__}"
            )
        return self


# ============================================================
# 最终输出类型：Claim 和 ClaimDocument
# ============================================================

class Claim(BaseModel):
    """
    最终的可验证主张。

    text 由 Claim Arbiter 从 anchor_ids 对应的 anchor.text 按序拼接重建。
    这是"不改写原文"的结构性保证——claim.text 永远等于原文锚点文本的精确拼接。

    原文位置由 source_locations 表达；承前法源位置由
    LegalSource.recognition.inherited_from 表达。
    """
    claim_id: str = Field(description="claim 唯一 ID，格式 cl_00001")
    claim_type: ClaimType = Field(description="主张类型")
    text: str = Field(description="从 anchors 重建的完整主张文本")
    anchor_ids: list[str] = Field(description="anchor 编号列表")
    entities: ClaimEntities = Field(description="实体信息")
    context_text: str = Field(
        default="",
        description="主张所在语义块的上下文；仅供检索",
    )
    source_locations: list[SourceLocation] = Field(
        default_factory=list,
        description="Word 或飞书中的原文定位坐标",
    )
    note_context: NoteContext | None = None
    model_config = ConfigDict(extra="forbid")
    @model_validator(mode="before")
    @classmethod
    def restore_entity_type(cls, data):
        return _coerce_entities(data)

    @model_validator(mode="after")
    def validate_entity_type(self):
        expected = _ENTITY_MODEL_BY_CLAIM_TYPE[self.claim_type]
        if not isinstance(self.entities, expected):
            raise ValueError(
                f"{self.claim_type.value} 必须使用 {expected.__name__}"
            )
        return self


class ClaimMeta(BaseModel):
    """引用文档元信息。"""
    schema_version: Literal["0.6"] = Field(
        default=CLAIM_SCHEMA_VERSION,
        description="schema 版本号",
    )
    claim_doc_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="claim 文档唯一 ID（uuid4）"
    )
    source_doc_id: str = Field(default="", description="来源 ParsedDocument 的 doc_id")
    source_doc_hash: str = Field(default="", description="来源文件 SHA-256 摘要")
    source_file: str = Field(default="", description="原始 DOCX 文件名")
    extracted_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="抽取时间（ISO-8601）"
    )
    extractor_version: Literal["0.5"] = Field(
        default=CLAIM_EXTRACTOR_VERSION,
        description="抽取器版本",
    )
    model_config = ConfigDict(extra="forbid")


class ClaimDocument(BaseModel):
    """
    CCiteheck 产出的顶层引用结构。

    每次抽取视为一次快照（snapshot）。
    不包含 anchor_range、source_anchor_ids、verification_status、
    normalized_text、confidence、primary_method、needs_review 等字段。
    """
    claim_meta: ClaimMeta = Field(default_factory=ClaimMeta)
    document_text: str = Field(
        default="",
        description="按文档阅读顺序拼接的全文，供全篇一致性和格式核查使用",
    )
    claims: list[Claim] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def require_serialized_version(cls, data):
        """外部 JSON 必须显式声明版本；内部构造使用 ClaimMeta 对象。"""
        if not isinstance(data, dict):
            return data
        meta = data.get("claim_meta")
        if isinstance(meta, ClaimMeta):
            return data
        if not isinstance(meta, dict):
            raise ValueError("claim_meta 缺失，无法确认 Claim schema 版本")
        missing = [
            field for field in ("schema_version", "extractor_version")
            if field not in meta
        ]
        if missing:
            raise ValueError("claim_meta 缺少版本字段: " + ", ".join(missing))
        return data
