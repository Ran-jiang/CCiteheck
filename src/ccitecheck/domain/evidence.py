"""法律溯源产生的证据与数据源轨迹模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class SourceTier(str, Enum):
    LOCAL_SQLITE = "local_sqlite"
    PKULAW_FALLBACK = "pkulaw_fallback"
    EURLEX = "eurlex"
    ANSVAR = "ansvar"
    FTC_OFFICIAL = "ftc_official"


class LookupStatus(str, Enum):
    ARTICLE_FOUND = "article_found"
    RELEVANT_ARTICLES_FOUND = "relevant_articles_found"
    LAW_FOUND_ARTICLE_MISSING = "law_found_article_missing"
    LAW_FOUND_TEXT_UNAVAILABLE = "law_found_text_unavailable"
    LAW_NOT_FOUND = "law_not_found"
    SOURCE_NOT_CONFIGURED = "source_not_configured"
    SOURCE_ERROR = "source_error"
    NOT_VERIFIABLE = "not_verifiable"
    OUT_OF_SCOPE = "out_of_scope"


class RetrievalStatus(str, Enum):
    FOUND = "found"
    LAW_NOT_FOUND = "law_not_found"
    ARTICLE_MISSING = "article_missing"
    TEXT_UNAVAILABLE = "text_unavailable"
    SOURCE_NOT_CONFIGURED = "source_not_configured"
    SOURCE_ERROR = "source_error"


class TechnicalStatus(str, Enum):
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    MODEL_ERROR = "model_error"
    SOURCE_ERROR = "source_error"
    SKIPPED = "skipped"


class CaseLookupStatus(str, Enum):
    VERIFIED = "verified"
    NOT_FOUND = "not_found"
    MANUAL_REVIEW = "manual_review"
    SOURCE_NOT_CONFIGURED = "source_not_configured"
    SOURCE_ERROR = "source_error"
    OUT_OF_SCOPE = "out_of_scope"


class SourceTrace(BaseModel):
    tier: SourceTier
    source_name: str
    source_url: Optional[str] = None
    fetched_at: Optional[str] = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: LookupStatus
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArticleExcerpt(BaseModel):
    law_title: str | None = None
    version_key: str | None = None
    source_url: str | None = None
    article_no: str = ""
    locator: str | None = None
    locator_type: Literal["article", "paragraph"] = "article"
    article_text: str
    relevance_score: float = Field(ge=0)


class ArticleEvidence(BaseModel):
    law_title: str
    source_type: Optional[str] = None
    article_no: Optional[str] = None
    article_text: Optional[str] = None
    version_label: Optional[str] = None
    version_status: Optional[str] = None
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    related_articles: list[ArticleExcerpt] = Field(default_factory=list)
    structure_path: Optional[str] = None
    data_source: SourceTrace


class CaseEvidence(BaseModel):
    matched_text: str
    case_number: str
    gid: str
    court: str = ""
    title: str = ""
    last_instance_date: Optional[str] = None
    url: Optional[str] = None
    holding: Optional[str] = None


class CaseSourceTrace(BaseModel):
    source_name: str
    source_url: Optional[str] = None
    fetched_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: CaseLookupStatus
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalAttempt(BaseModel):
    mode: str
    status: str
    request: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceCandidate(BaseModel):
    candidate_id: str = Field(default_factory=lambda: str(uuid4()))
    kind: Literal["statute", "case", "metadata"]
    title: str | None = None
    locator: str | None = None
    text: str | None = None
    source_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetrievalEvidence(BaseModel):
    evidence_id: str = Field(default_factory=lambda: str(uuid4()))
    claim_id: str
    hypothesis_id: str
    source_id: str
    submitted_request: dict[str, Any] = Field(default_factory=dict)
    route_attempts: list[RetrievalAttempt] = Field(default_factory=list)
    retrieval_status: RetrievalStatus
    technical_status: TechnicalStatus
    candidates: list[EvidenceCandidate] = Field(default_factory=list)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    provider_normalized_query: str | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)


__all__ = [
    "ArticleEvidence",
    "ArticleExcerpt",
    "CaseEvidence",
    "CaseLookupStatus",
    "CaseSourceTrace",
    "EvidenceCandidate",
    "LookupStatus",
    "RetrievalAttempt",
    "RetrievalEvidence",
    "RetrievalStatus",
    "SourceTier",
    "SourceTrace",
    "TechnicalStatus",
]
