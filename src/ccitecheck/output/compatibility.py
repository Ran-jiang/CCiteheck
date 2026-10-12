"""内部核验状态到现有 Word 契约的唯一映射。"""

from ..domain.verification import VerificationStatus


def word_outcome(status: VerificationStatus) -> str:
    return {
        VerificationStatus.MATCH: "pass",
        VerificationStatus.MISMATCH: "issue",
        VerificationStatus.INSUFFICIENT_EVIDENCE: "bug",
    }[status]


__all__ = ["word_outcome"]
