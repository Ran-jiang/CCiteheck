"""RawClaim 与 RetrievalEvidence 的纯比较入口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.citation import ClaimType
from ..domain.claims import RawClaim
from ..domain.evidence import RetrievalEvidence, RetrievalStatus, TechnicalStatus
from ..domain.verification import (
    DimensionResult,
    VerificationResult,
    VerificationStatus,
    overall_status,
)
from ..infrastructure.database import normalize_article_key
from ..query_construction.matching import equivalent_law_titles
from .cases.identity import (
    equivalent_case_name,
    normalize_case_number,
    same_court,
)


class ContentComparator(Protocol):
    def __call__(self, claim_text: str, evidence_text: str) -> DimensionResult: ...


@dataclass(frozen=True)
class VerificationContext:
    content_comparator: ContentComparator | None = None


def verify(
    claim: RawClaim,
    evidence: RetrievalEvidence,
    context: VerificationContext | None = None,
) -> VerificationResult:
    """只比较既有原文与证据；不检索、不重试、不改变 hypothesis。"""
    context = context or VerificationContext()
    if (
        evidence.technical_status != TechnicalStatus.COMPLETED
        or evidence.retrieval_status != RetrievalStatus.FOUND
        or not evidence.candidates
    ):
        dimensions = {"evidence": DimensionResult(
            status=VerificationStatus.INSUFFICIENT_EVIDENCE,
            reason_codes=[evidence.retrieval_status.value],
        )}
        return VerificationResult(
            claim_id=claim.claim_id,
            hypothesis_id=evidence.hypothesis_id,
            evidence_id=evidence.evidence_id,
            overall_status=VerificationStatus.INSUFFICIENT_EVIDENCE,
            technical_status=evidence.technical_status,
            dimensions=dimensions,
        )

    if claim.case_mentions:
        return _verify_case(claim, evidence, context)

    candidate = evidence.candidates[0]
    mention = claim.legal_mentions[0] if claim.legal_mentions else None
    raw_title = (mention.raw_title or mention.raw_title_candidate) if mention else None
    identity_matches = bool(
        raw_title
        and candidate.title
        and equivalent_law_titles(raw_title, candidate.title)
    )
    dimensions = {"source_identity": DimensionResult(
        status=(VerificationStatus.MATCH if identity_matches else VerificationStatus.MISMATCH),
        reason_codes=[] if identity_matches else ["source_identity_mismatch"],
        comparison={"raw": raw_title, "authoritative": candidate.title},
    )}
    if mention and mention.article_raw:
        locator_matches = (
            normalize_article_key(mention.article_raw)
            == normalize_article_key(candidate.locator or "")
        )
        dimensions["locator"] = DimensionResult(
            status=(VerificationStatus.MATCH if locator_matches else VerificationStatus.MISMATCH),
            reason_codes=[] if locator_matches else ["locator_mismatch"],
            comparison={"raw": mention.article_raw, "authoritative": candidate.locator},
        )
    if candidate.text:
        dimensions["content"] = (
            context.content_comparator(claim.raw_text, candidate.text)
            if context.content_comparator
            else DimensionResult(
                status=VerificationStatus.INSUFFICIENT_EVIDENCE,
                reason_codes=["content_comparison_not_requested"],
            )
        )
    return VerificationResult(
        claim_id=claim.claim_id,
        hypothesis_id=evidence.hypothesis_id,
        evidence_id=evidence.evidence_id,
        overall_status=overall_status(dimensions),
        dimensions=dimensions,
    )


def _verify_case(
    claim: RawClaim,
    evidence: RetrievalEvidence,
    context: VerificationContext,
) -> VerificationResult:
    mention = claim.case_mentions[0]
    candidates = [item for item in evidence.candidates if item.kind == "case"]
    if mention.raw_case_number:
        target = normalize_case_number(mention.raw_case_number)
        matches = [
            item for item in candidates
            if normalize_case_number(item.locator or "") == target
        ]
    elif mention.raw_case_name:
        matches = [
            item for item in candidates
            if item.title and equivalent_case_name(mention.raw_case_name, item.title)
        ]
    else:
        matches = []

    if len(matches) != 1:
        status = (
            VerificationStatus.MISMATCH
            if candidates and not matches
            else VerificationStatus.INSUFFICIENT_EVIDENCE
        )
        dimensions = {"case_identity": DimensionResult(
            status=status,
            reason_codes=[
                "case_identity_mismatch" if status == VerificationStatus.MISMATCH
                else "case_identity_ambiguous"
            ],
        )}
    else:
        candidate = matches[0]
        authoritative_court = str(candidate.metadata.get("court") or "")
        if not mention.raw_court:
            identity_status = VerificationStatus.MATCH
            identity_reasons = []
        elif not authoritative_court:
            identity_status = VerificationStatus.INSUFFICIENT_EVIDENCE
            identity_reasons = ["authoritative_court_unavailable"]
        elif same_court(mention.raw_court, authoritative_court):
            identity_status = VerificationStatus.MATCH
            identity_reasons = []
        else:
            identity_status = VerificationStatus.MISMATCH
            identity_reasons = ["case_court_mismatch"]
        dimensions = {"case_identity": DimensionResult(
            status=identity_status,
            reason_codes=identity_reasons,
            comparison={
                "raw_case_number": mention.raw_case_number,
                "raw_case_name": mention.raw_case_name,
                "authoritative_case_number": candidate.locator,
                "authoritative_case_name": candidate.title,
            },
        )}
        if claim.claim_type == ClaimType.CASE_HOLDING_PARAPHRASE:
            dimensions["holding_fidelity"] = (
                context.content_comparator(claim.raw_text, candidate.text)
                if context.content_comparator and candidate.text
                else DimensionResult(
                    status=VerificationStatus.INSUFFICIENT_EVIDENCE,
                    reason_codes=["holding_comparison_unavailable"],
                )
            )
    return VerificationResult(
        claim_id=claim.claim_id,
        hypothesis_id=evidence.hypothesis_id,
        evidence_id=evidence.evidence_id,
        overall_status=overall_status(dimensions),
        dimensions=dimensions,
    )


__all__ = ["VerificationContext", "verify"]
