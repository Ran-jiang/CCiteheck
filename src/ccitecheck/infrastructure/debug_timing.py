"""Per-run debug timing without coupling metrics to legal domain models."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import logging
from threading import Lock
import time
from typing import Callable, Iterator, ParamSpec, TypeVar


_LOGGER = logging.getLogger("uvicorn.error")
_CURRENT: ContextVar[DebugTimer | None] = ContextVar(
    "ccitecheck_debug_timer", default=None
)
_ACTIVE_MEASUREMENTS: ContextVar[tuple[str, ...]] = ContextVar(
    "ccitecheck_active_measurements", default=()
)
_P = ParamSpec("_P")
_R = TypeVar("_R")
_STAGE_ORDER = (
    "recognition.total",
    "recognition.check_item_preparation",
    "query_construction.total",
    "query.primary",
    "query.related_planner",
    "query.repair_planner",
    "retrieval.total",
    "retrieval.pkulaw_keyword",
    "retrieval.pkulaw_semantic",
    "retrieval.pkulaw_recognition",
    "retrieval.pkulaw_law_item",
    "retrieval.pkulaw_nearby_scan",
    "retrieval.pkulaw_queue_wait",
    "retrieval.reranker",
    "location.triage_llm",
    "comparison.total",
    "comparison.llm",
    "case_verification.total",
    "output",
)


@dataclass(frozen=True)
class TimingStat:
    seconds: float
    calls: int


class DebugTimer:
    """Thread-safe accumulated timings for one verification run."""

    def __init__(self) -> None:
        self._started = time.perf_counter()
        self._finished: float | None = None
        self._stats: dict[str, list[float | int]] = defaultdict(lambda: [0.0, 0])
        self._lock = Lock()

    @contextmanager
    def activate(self) -> Iterator[DebugTimer]:
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(token)

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        active = _ACTIVE_MEASUREMENTS.get()
        if name in active:
            yield
            return
        token = _ACTIVE_MEASUREMENTS.set((*active, name))
        started = time.perf_counter()
        try:
            yield
        finally:
            _ACTIVE_MEASUREMENTS.reset(token)
            elapsed = time.perf_counter() - started
            with self._lock:
                stat = self._stats[name]
                stat[0] += elapsed
                stat[1] += 1

    def bind(self, operation: Callable[_P, _R]) -> Callable[_P, _R]:
        """Propagate this run's timer into a worker thread."""
        active = _ACTIVE_MEASUREMENTS.get()

        @wraps(operation)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            token = _ACTIVE_MEASUREMENTS.set(active)
            try:
                with self.activate():
                    return operation(*args, **kwargs)
            finally:
                _ACTIVE_MEASUREMENTS.reset(token)

        return wrapped

    def finish(self) -> None:
        if self._finished is None:
            self._finished = time.perf_counter()

    @property
    def total_seconds(self) -> float:
        end = self._finished if self._finished is not None else time.perf_counter()
        return end - self._started

    def snapshot(self) -> dict[str, TimingStat]:
        with self._lock:
            return {
                name: TimingStat(seconds=float(value[0]), calls=int(value[1]))
                for name, value in self._stats.items()
            }

    def format_report(self) -> str:
        stats = self.snapshot()
        width = max([len(name) for name in stats] + [len("TOTAL")])
        order = {name: index for index, name in enumerate(_STAGE_ORDER)}
        rows = ["[CCITECHECK TIMING]"]
        rows.extend(
            f"{name:<{width}}  {stat.seconds:7.2f}s  calls={stat.calls}"
            for name, stat in sorted(
                stats.items(), key=lambda item: (order.get(item[0], len(order)), item[0])
            )
        )
        rows.append(f"{'TOTAL':<{width}}  {self.total_seconds:7.2f}s  calls=1")
        return "\n".join(rows)

    def emit(self) -> None:
        self.finish()
        _LOGGER.info("\n%s", self.format_report())


@contextmanager
def timing_session() -> Iterator[DebugTimer]:
    """Reuse an outer timer, or create and emit one for this run."""

    active = _CURRENT.get()
    if active is not None:
        yield active
        return
    timer = DebugTimer()
    try:
        with timer.activate():
            yield timer
    finally:
        timer.emit()


def measure(name: str):
    timer = _CURRENT.get()
    return timer.measure(name) if timer is not None else nullcontext()


def bind_current_timer(
    operation: Callable[_P, _R],
) -> Callable[_P, _R]:
    timer = _CURRENT.get()
    return timer.bind(operation) if timer is not None else operation


__all__ = [
    "DebugTimer",
    "TimingStat",
    "bind_current_timer",
    "measure",
    "timing_session",
]
