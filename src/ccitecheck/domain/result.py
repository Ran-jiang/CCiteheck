"""面向 API 与前端的法规、案例核验文档。"""

from datetime import datetime, timezone

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .case_results import CaseVerificationResult
from .statute_results import StatuteVerificationResult

VERIFICATION_SCHEMA_VERSION = "0.9"


class FrontendVerificationDocument(BaseModel):
    schema_version: Literal["0.9"]
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_claim_doc_id: str
    statute_results: list[StatuteVerificationResult] = Field(default_factory=list)
    case_results: list[CaseVerificationResult] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


__all__ = ["FrontendVerificationDocument", "VERIFICATION_SCHEMA_VERSION"]
