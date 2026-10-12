"""司法案例核验专用结果模型。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .citation import NoteContext, SourceLocation
from .evidence import CaseEvidence, CaseLookupStatus, CaseSourceTrace
from .revisions import RevisionProposal
from .checks import CheckVerdict, ExecutionStatus


class CaseErrorCode(str, Enum):
    CASE_NOT_FOUND = "case_not_found"
    CASE_IDENTITY_ERROR = "case_identity_error"
    HOLDING_UNSUPPORTED = "holding_unsupported"
    HOLDING_DISTORTED = "holding_distorted"


class CaseFinding(BaseModel):
    code: CaseErrorCode
    risk_level: Literal["HIGH", "MEDIUM"]
    summary: str = Field(max_length=300)
    suggestion: str
    matched_excerpt: str | None = Field(
        default=None,
        description="命中的裁判说理原文句段，程序按句号从权威文本取回，前后以省略号标示节选",
    )
    revision: RevisionProposal | None = None


class CaseHoldingCheck(BaseModel):
    execution_status: ExecutionStatus = ExecutionStatus.COMPLETED
    verdict: CheckVerdict | None = None
    findings: list[CaseFinding] = Field(default_factory=list)
    notes: str = ""
    error_code: str | None = None
    retryable: bool = False
    skipped_reason: str | None = None


class CaseCandidate(BaseModel):
    title: str
    case_number: str | None = None
    court: str | None = None
    last_instance_date: str | None = None
    url: str | None = None


class CaseVerificationResult(BaseModel):
    check_id: str
    display_group_id: str = Field(min_length=1)
    claim_id: str
    claim_text: str
    jurisdiction: str = "CN"
    cited_case_number: str | None = None
    cited_case_name: str | None = None
    cited_court: str | None = None
    lookup_status: CaseLookupStatus
    evidence: CaseEvidence | None = None
    findings: list[CaseFinding] = Field(default_factory=list)
    outcome: Literal["pass", "issue", "bug"]
    message: str = ""
    holding_check: CaseHoldingCheck | None = None
    source_locations: list[SourceLocation] = Field(default_factory=list)
    note_context: NoteContext | None = None
    source_attempts: list[CaseSourceTrace] = Field(default_factory=list)
    candidate_cases: list[CaseCandidate] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


__all__ = [
    "CaseErrorCode",
    "CaseCandidate",
    "CaseFinding",
    "CaseHoldingCheck",
    "CaseVerificationResult",
]
