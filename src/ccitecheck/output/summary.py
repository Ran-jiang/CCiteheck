"""汇总法规、案例专属核验结果。"""

from pydantic import BaseModel

from ..domain.evidence import CaseLookupStatus
from ..domain.result import FrontendVerificationDocument


class VerificationSummary(BaseModel):
    total: int
    card_total: int
    reference_total: int
    passed: int
    issues: int
    reviews: int
    bugs: int
    cases_verified: int
    cases_not_found: int


def summarize_verification(verification: FrontendVerificationDocument) -> VerificationSummary:
    results = [*verification.statute_results, *verification.case_results]
    display_groups = {
        result.display_group_id or result.claim_id
        for result in results
    }
    return VerificationSummary(
        total=len(results),
        card_total=len(display_groups),
        reference_total=len(results),
        passed=sum(result.outcome == "pass" for result in results),
        issues=sum(result.outcome == "issue" for result in results),
        reviews=sum(result.outcome == "review" for result in results),
        bugs=sum(result.outcome == "bug" for result in results),
        cases_verified=sum(
            result.lookup_status == CaseLookupStatus.VERIFIED
            for result in verification.case_results
        ),
        cases_not_found=sum(
            result.lookup_status == CaseLookupStatus.NOT_FOUND
            for result in verification.case_results
        ),
    )


__all__ = ["VerificationSummary", "summarize_verification"]
