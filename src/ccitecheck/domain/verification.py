"""证据比较结果模型；不含数据源执行逻辑。"""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .evidence import TechnicalStatus


class VerificationStatus(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class DimensionResult(BaseModel):
    status: VerificationStatus
    reason_codes: list[str] = Field(default_factory=list)
    comparison: dict[str, Any] | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)


class VerificationResult(BaseModel):
    verification_id: str = Field(default_factory=lambda: str(uuid4()))
    claim_id: str
    hypothesis_id: str
    evidence_id: str
    overall_status: VerificationStatus
    technical_status: TechnicalStatus = TechnicalStatus.COMPLETED
    dimensions: dict[str, DimensionResult] = Field(default_factory=dict)
    candidate_hints: list[dict[str, Any]] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", frozen=True)


def overall_status(dimensions: dict[str, DimensionResult]) -> VerificationStatus:
    statuses = [item.status for item in dimensions.values()]
    if VerificationStatus.MISMATCH in statuses:
        return VerificationStatus.MISMATCH
    if VerificationStatus.INSUFFICIENT_EVIDENCE in statuses or not statuses:
        return VerificationStatus.INSUFFICIENT_EVIDENCE
    return VerificationStatus.MATCH


__all__ = [
    "DimensionResult",
    "VerificationResult",
    "VerificationStatus",
    "overall_status",
]
