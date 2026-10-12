"""把领域核查结果转换为摘要和前端数据。"""

from .summary import VerificationSummary, summarize_verification
from .compatibility import word_outcome
from .service import render_word_result

__all__ = [
    "VerificationSummary",
    "render_word_result",
    "summarize_verification",
    "word_outcome",
]
