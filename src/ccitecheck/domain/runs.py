"""Scheduler 的流程历史与下一步决策模型。"""

from __future__ import annotations

from enum import Enum
import os
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


# 单条 RunState 机器与文档 _CheckItem 流水线共用的重试预算。
DEFAULT_TECHNICAL_RETRY_LIMIT = _env_int("CCITE_TECHNICAL_RETRIES", 2)
DEFAULT_HYPOTHESIS_RETRY_LIMIT = _env_int("CCITE_HYPOTHESIS_RETRIES", 3)


class RunState(str, Enum):
    NEW = "new"
    HYPOTHESIS_READY = "hypothesis_ready"
    RETRIEVING = "retrieving"
    EVIDENCE_READY = "evidence_ready"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    OUTPUT = "output"
    STOPPED = "stopped"


_ALLOWED_TRANSITIONS = {
    RunState.NEW: {RunState.HYPOTHESIS_READY},
    RunState.HYPOTHESIS_READY: {RunState.RETRIEVING, RunState.STOPPED},
    RunState.RETRIEVING: {
        RunState.EVIDENCE_READY,
        RunState.HYPOTHESIS_READY,
        RunState.STOPPED,
    },
    RunState.EVIDENCE_READY: {RunState.VERIFYING},
    RunState.VERIFYING: {RunState.VERIFIED},
    RunState.VERIFIED: {
        RunState.RETRIEVING,
        RunState.HYPOTHESIS_READY,
        RunState.OUTPUT,
        RunState.STOPPED,
    },
    RunState.OUTPUT: set(),
    RunState.STOPPED: set(),
}


class SourceAttempt(BaseModel):
    source_id: str
    hypothesis_id: str
    evidence_id: str | None = None
    technical_status: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class SchedulerDecision(BaseModel):
    kind: Literal[
        "output", "next_source", "technical_retry", "rebuild_hypothesis", "stop"
    ]
    reason: str
    source_id: str | None = None
    feedback: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid", frozen=True)


class VerificationRun(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    claim_id: str
    state: RunState = RunState.NEW
    hypothesis_history: list[str] = Field(default_factory=list)
    evidence_history: list[str] = Field(default_factory=list)
    verification_history: list[str] = Field(default_factory=list)
    source_attempts: list[SourceAttempt] = Field(default_factory=list)
    technical_retry_count: int = 0
    hypothesis_retry_count: int = 0
    max_technical_retries: int = DEFAULT_TECHNICAL_RETRY_LIMIT
    max_hypothesis_retries: int = DEFAULT_HYPOTHESIS_RETRY_LIMIT
    terminal_reason: str | None = None
    model_config = ConfigDict(extra="forbid")

    def transition(self, target: RunState) -> None:
        if target == self.state:
            return
        if target not in _ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"invalid run transition: {self.state.value} -> {target.value}")
        self.state = target


__all__ = [
    "DEFAULT_HYPOTHESIS_RETRY_LIMIT",
    "DEFAULT_TECHNICAL_RETRY_LIMIT",
    "RunState",
    "SchedulerDecision",
    "SourceAttempt",
    "VerificationRun",
]
