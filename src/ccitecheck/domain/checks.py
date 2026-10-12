"""法规和案例核验共享的执行状态值。"""

from enum import Enum


class CheckVerdict(str, Enum):
    PASS = "pass"
    ISSUE = "issue"
    INSUFFICIENT_INPUT = "insufficient_input"


class ExecutionStatus(str, Enum):
    COMPLETED = "completed"
    LLM_ERROR = "llm_error"
    SKIPPED = "skipped"


__all__ = ["CheckVerdict", "ExecutionStatus"]
