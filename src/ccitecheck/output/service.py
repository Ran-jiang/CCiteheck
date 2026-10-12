"""把已确定结果组装为 Word 前端 DTO。"""

from __future__ import annotations

from ..domain.claims import RawClaim
from ..domain.case_results import CaseVerificationResult
from ..domain.result import FrontendVerificationDocument, VERIFICATION_SCHEMA_VERSION
from ..domain.runs import VerificationRun
from ..domain.statute_results import StatuteVerificationResult


def render_word_result(
    claim: RawClaim,
    run: VerificationRun,
    *,
    statute_results: list[StatuteVerificationResult] | None = None,
    case_results: list[CaseVerificationResult] | None = None,
) -> FrontendVerificationDocument:
    """只做 DTO 组装；结果内容必须由 Verification 已经确定。"""
    return FrontendVerificationDocument(
        schema_version=VERIFICATION_SCHEMA_VERSION,
        source_claim_doc_id=claim.claim_id,
        statute_results=statute_results or [],
        case_results=case_results or [],
    )


__all__ = ["render_word_result"]
