"""基于已确认法条执行法律适用核查。"""

from __future__ import annotations

import logging

from ..domain.checks import ExecutionStatus
from ..infrastructure.debug_timing import measure
from ..domain.statute_results import LegalApplicationCheck
from .legal_application import ApplicationAuthority
from .semantic import SemanticCheckError, SemanticChecker


_LOGGER = logging.getLogger(__name__)

def compare_application_with_llm(
    semantic_checker: SemanticChecker,
    original_text: str,
    authorities: list[ApplicationAuthority],
) -> LegalApplicationCheck:
    try:
        with measure("comparison.llm"):
            return semantic_checker.compare_application(original_text, authorities)
    except SemanticCheckError as exc:
        error_code = getattr(exc, "error_code", "semantic_error")
        _LOGGER.exception("法律适用模型调用失败：%s", exc)
        return LegalApplicationCheck(
            execution_status=ExecutionStatus.LLM_ERROR,
            notes="模型服务暂时不可用",
            error_code="model_unavailable",
            retryable=error_code in {
                "transport_error", "timeout", "rate_limited", "upstream_error",
            },
        )


__all__ = [
    "compare_application_with_llm",
]
