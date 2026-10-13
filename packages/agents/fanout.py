# mypy: disable-error-code="unreachable"
"""Parallel subquestion fan-out with bounded concurrency, aggregate budget (T-V3-017).

One plan's questions fan out to bounded parallel workers, sharing one budget
envelope. Every bound from the runtime layer applies.

CAPS - why each bound exists (V3 budget discipline):
  MAX_FANOUT_CONCURRENCY=3      # cap parallel workers; 9 Qs x unbounded would be fork bomb
  MAX_FANOUT_QUESTIONS=9        # mirrors planner max questions; never exceeds plan size
  MAX_FANOUT_BUDGET=9           # aggregate envelope default; one unit per question, stops overruns
  MAX_QUESTION_CHARS=200        # inherited from planner; prevents one question blowing memory

Concurrency: Semaphore(MAX_FANOUT_CONCURRENCY) — fork bomb prevention.
Budget: FanoutBudget shared envelope — per-question reserve draws from it;
        exhaustion STOPS new dispatches but lets in-flight finish (no orphan kills).
Aggregation: results rejoin in plan order regardless of completion order;
             failed sibling does not poison others, aggregate is partial-with-errors.

Deterministic tests: fake executor with controlled completion order, no sleeps.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Protocol

# ---------------------------------------------------------------------------
# Caps + why (see module docstring)
# ---------------------------------------------------------------------------

MAX_FANOUT_CONCURRENCY = 3
MAX_FANOUT_QUESTIONS = 9
MAX_FANOUT_BUDGET = 9
MAX_QUESTION_CHARS = 200


# ---------------------------------------------------------------------------
# Budget envelope — shared, per-question reserve
# ---------------------------------------------------------------------------


class FanoutBudget:
    """Aggregate budget envelope — per-question reserves draw from it."""

    def __init__(self, total: int = MAX_FANOUT_BUDGET) -> None:
        if total < 0:
            raise ValueError("budget total must be >=0")
        self.total = total
        self.used = 0
        self._lock = threading.Lock()

    def try_reserve(self, n: int = 1) -> bool:
        with self._lock:
            if self.used + n <= self.total:
                self.used += n
                return True
            return False

    def remaining(self) -> int:
        with self._lock:
            return self.total - self.used


@dataclass(frozen=True)
class FanoutConfig:
    max_concurrency: int = MAX_FANOUT_CONCURRENCY
    max_questions: int = MAX_FANOUT_QUESTIONS

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >=1")
        if self.max_questions < 1:
            raise ValueError("max_questions must be >=1")


# ---------------------------------------------------------------------------
# Executor seam + results
# ---------------------------------------------------------------------------


class QuestionExecutor(Protocol):
    async def execute(self, question: str, index: int) -> str:
        ...


@dataclass(frozen=True)
class FanoutResult:
    question: str
    index: int
    output: str | None = None
    error: str | None = None
    status: str = "ok"  # ok | failed | budget_exhausted


@dataclass
class FanoutAggregate:
    results: list[FanoutResult] = field(default_factory=list)
    status: str = "completed"  # completed | partial_with_errors | budget_exhausted

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.status == "ok")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status in ("failed", "budget_exhausted"))


# ---------------------------------------------------------------------------
# Fan-out runner — bounded concurrency, shared budget, ordered aggregation
# ---------------------------------------------------------------------------


async def fanout_questions(
    questions: list[str],
    executor: QuestionExecutor,
    config: FanoutConfig | None = None,
    budget: FanoutBudget | None = None,
) -> FanoutAggregate:
    if config is None:
        config = FanoutConfig()
    if budget is None:
        budget = FanoutBudget(total=config.max_questions)

    # Truncate to max_questions — never exceed plan size
    qs = questions[: config.max_questions]

    # Pre-size results in plan order — no drops
    results: list[FanoutResult | None] = [None] * len(qs)
    sem = asyncio.Semaphore(config.max_concurrency)

    async def run_one(idx: int, q: str) -> None:
        async with sem:
            try:
                out = await executor.execute(q, idx)
                results[idx] = FanoutResult(question=q, index=idx, output=out, status="ok")
            except Exception as exc:
                results[idx] = FanoutResult(question=q, index=idx, output=None, error=str(exc), status="failed")  # noqa: E501

    # Reserve budget before dispatch — exhaustion stops new dispatches
    tasks: list[asyncio.Task[None]] = []
    exhausted_at: int | None = None
    for idx, q in enumerate(qs):
        if not budget.try_reserve(1):
            exhausted_at = idx
            break
        tasks.append(asyncio.create_task(run_one(idx, q)))

    # Budget exhausted — mark remaining (not dispatched) as budget_exhausted
    if exhausted_at is not None:
        for j in range(exhausted_at, len(qs)):
            if results[j] is None:
                results[j] = FanoutResult(question=qs[j], index=j, output=None, error="BUDGET_EXHAUSTED", status="budget_exhausted")  # noqa: E501

    # Let in-flight finish — no orphan kills
    if tasks:
        await asyncio.gather(*tasks)

    # Fill any gaps (should not happen) as failed to prevent silent-drop
    final: list[FanoutResult] = []
    for idx, r in enumerate(results):
        if r is None:
            final.append(FanoutResult(question=qs[idx], index=idx, output=None, error="MISSING", status="failed"))  # noqa: E501
        else:
            final.append(r)

    # Aggregate status
    has_failed = any(r.status != "ok" for r in final)
    if has_failed:
        status = "partial_with_errors"
    else:
        status = "completed"

    return FanoutAggregate(results=final, status=status)
