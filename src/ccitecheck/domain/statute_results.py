"""法规核验专用结果模型。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .citation import NoteContext, SourceLocation
from .evidence import ArticleEvidence, LookupStatus, SourceTrace
from .checks import ExecutionStatus
from .revisions import RevisionProposal


class StatuteErrorCode(str, Enum):
    FORMAT_ERROR = "format_error"
    SOURCE_NOT_FOUND = "source_not_found"
    LAW_NAME_ERROR = "law_name_error"
    ARTICLE_NOT_FOUND = "article_not_found"
    ARTICLE_NUMBER_ERROR = "article_number_error"
    CITATION_HIERARCHY_ERROR = "citation_hierarchy_error"
    SOURCE_REPEALED = "source_repealed"
    SOURCE_AMENDED = "source_amended"


class StatuteLocator(BaseModel):
    article_no: str | None = None
    paragraph_no: str | None = None
    item_no: str | None = None


class StatuteVersion(BaseModel):
    version_key: str
    version_label: str | None = None
    version_status: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    article_no: str
    article_text: str


class StructuredItem(BaseModel):
    item_no: str
    text: str


class StructuredParagraph(BaseModel):
    paragraph_no: str
    text: str
    items: list[StructuredItem] = Field(default_factory=list)
    introduction: str = ""


class StructuredArticle(BaseModel):
    article_no: str
    raw_text: str
    paragraph_boundaries_reliable: bool
    paragraphs: list[StructuredParagraph] = Field(min_length=1)


class StatuteLocationCandidate(BaseModel):
    locator: StatuteLocator
    text: str
    source_url: str | None = None
    confirmed_level: Literal["article", "paragraph", "item"] | None = None
    evidence_spans: list[tuple[int, int]] = Field(default_factory=list)
    coverage: float = Field(default=0, ge=0, le=1)
    supported: bool = False


class StatuteLocationResolution(BaseModel):
    status: Literal["resolved", "candidates_pending", "not_found"]
    candidates: list[StatuteLocationCandidate] = Field(default_factory=list)
    source_trace: SourceTrace | None = None


class StatuteFinding(BaseModel):
    code: StatuteErrorCode
    risk_level: Literal["HIGH", "MEDIUM"]
    summary: str = Field(max_length=300)
    suggestion: str
    cited_locator: StatuteLocator | None = None
    resolved_locator: StatuteLocator | None = None
    historical_version: StatuteVersion | None = None
    revision: RevisionProposal | None = None


class LegalApplicationReview(BaseModel):
    """法律适用核查只提出人工复核事项，不直接判定未通过。"""

    error_type: Literal[
        "rule_fact_mismatch",
        "meaning_distorted",
    ]
    review_level: Literal["待核查"] = "待核查"
    summary: str = Field(max_length=300)
    suggestion: str
    related_sources: list[str] = Field(default_factory=list)


class LegalApplicationCheck(BaseModel):
    execution_status: ExecutionStatus = ExecutionStatus.COMPLETED
    verdict: Literal["pass", "review"] | None = None
    reviews: list[LegalApplicationReview] = Field(default_factory=list)
    comparison: str = Field(default="", max_length=300)
    notes: str = ""
    error_code: str | None = None
    retryable: bool = False
    job_id: str | None = None


class StatuteVerificationResult(BaseModel):
    check_id: str
    card_id: str
    display_group_id: str = Field(min_length=1)
    claim_id: str
    claim_text: str
    law_title: str
    recognition_form: Literal["explicit", "bare", "inherited"] = "explicit"
    law_identity_resolved: bool = True
    law_identity_resolver: Literal[
        "direct", "structure", "lexicon", "context", "pkulaw"
    ] | None = None
    jurisdiction: str = "CN"
    cited_locators: list[StatuteLocator] = Field(default_factory=list)
    lookup_status: LookupStatus
    evidence: ArticleEvidence | None = None
    correction_evidence: ArticleEvidence | None = None
    location_resolution: StatuteLocationResolution | None = None
    findings: list[StatuteFinding] = Field(default_factory=list)
    outcome: Literal["pass", "issue", "review", "bug"]
    message: str = ""
    application_check: LegalApplicationCheck | None = None
    reference_role: Literal["direct", "nested", "carry_forward"] = "direct"
    parent_check_id: str | None = None
    relation_status: Literal[
        "confirmed", "parent_failed", "parent_unavailable",
        "locator_mismatch", "resolved", "insufficient",
    ] | None = None
    relation_message: str = ""
    source_locations: list[SourceLocation] = Field(default_factory=list)
    note_context: NoteContext | None = None
    source_attempts: list[SourceTrace] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


__all__ = [
    "StatuteErrorCode",
    "StatuteFinding",
    "StatuteLocator",
    "StatuteLocationCandidate",
    "StatuteLocationResolution",
    "LegalApplicationCheck",
    "LegalApplicationReview",
    "StatuteVersion",
    "StructuredArticle",
    "StructuredItem",
    "StructuredParagraph",
    "StatuteVerificationResult",
]
