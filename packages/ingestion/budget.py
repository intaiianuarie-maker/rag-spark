"""Parse budgets enforced **before** a document finishes expanding (T-V1-031).

Why this exists
---------------
The old guard measured *elements after parse* (``ensure_element_limit``). By the
time it could refuse, a pathological input had already spent its time, its
memory and its text — the check was a verdict on work already done, not a
budget. T-V1-031 requires the budgets to be enforced **pre-expansion**: the
parse must stop *while* it is producing elements, not after it produced them.

Four budgets, one per resource the ticket names (all env-tunable, all
documented in ``docs/ingestion/FORMAT-MATRIX.md``):

===========================  ==========================================
``max_seconds``              wall clock for one parse (hard abandonment)
``max_memory_bytes``         peak RSS observed while the parse runs
``max_expanded_chars``       characters produced across every element
``max_output_elements``      elements produced
===========================  ==========================================

How enforcement reaches every adapter without touching each loop
----------------------------------------------------------------
Every PD-001 adapter builds its tree through
:func:`ingestion.normalize.make_element` — that is the single choke point, and
it now calls :meth:`ParseBudgetGuard.observe` per element. The budget therefore
trips **between** element ``n`` and element ``n+1``: expansion stops, the third
party parser unwinds, and no ``ParsedDocument`` is ever returned.

Honest limits (disclosed, not hidden):

- A budget can only trip where a checkpoint runs. ``observe`` is the
  pre-expansion checkpoint; :func:`parse_with_budgets` additionally enforces
  the wall clock with a timeout and re-checks the accounting when the parse
  returns, so a parser that blocks *before* emitting anything is still
  abandoned at the deadline.
- Python cannot kill a thread blocked inside a C call. The timeout **abandons**
  that thread; the abandoned parse is tripped by the guard at its next
  checkpoint (which is what makes abandonment effective for the expansion-bomb
  case this budget exists for). A call wedged inside a third-party C routine
  keeps its pool slot until it returns — that is disclosed in the docs page
  rather than claimed away.

No third-party code: stdlib only.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from .interfaces import DocumentParser, ParsedDocument

#: Parse-time defaults (see ``docs/ingestion/FORMAT-MATRIX.md``).
DEFAULT_PARSE_TIMEOUT_SECONDS = 120.0
DEFAULT_PARSE_MAX_MEMORY_MB = 1024
DEFAULT_PARSE_MAX_EXPANDED_CHARS = 2_000_000
#: Mirrors ``jobs.ingest_pipeline.DEFAULT_MAX_ELEMENTS`` so the guard and the
#: post-parse backstop agree on one number (the backstop stays: two independent
#: guards with the same limit is not the same as one).
DEFAULT_PARSE_MAX_OUTPUT_ELEMENTS = 100_000

#: One parse at a time per worker job; a couple of slots so an abandoned parse
#: cannot wedge the pool on its own (see the module docstring's disclosed limit).
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="parse-budget")


class ParseBudgetExceeded(Exception):
    """A parse budget was exhausted.

    Deliberately *not* an :class:`ingestion.normalize.IngestionError`: the
    guard must be importable from ``normalize`` (which owns
    ``IngestionError``) without a circular import. Adapters re-raise it
    verbatim so the pipeline sees the budget reason instead of a generic
    ``parse_error``.

    ``code`` is the exact non-retryable pipeline code:
    ``parse_time_budget`` / ``parse_memory_budget`` /
    ``parse_expanded_text_budget`` / ``parse_output_budget``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


TIME_BUDGET = "parse_time_budget"
MEMORY_BUDGET = "parse_memory_budget"
EXPANDED_TEXT_BUDGET = "parse_expanded_text_budget"
OUTPUT_BUDGET = "parse_output_budget"

#: Every budget code is deterministic: retrying can only spend the same budget
#: again, so the pipeline must park these as non-retryable.
BUDGET_CODES: frozenset[str] = frozenset(
    {TIME_BUDGET, MEMORY_BUDGET, EXPANDED_TEXT_BUDGET, OUTPUT_BUDGET}
)


def _positive_float(raw: str | None, default: float) -> float:
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _positive_int(raw: str | None, default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class ParseBudgets:
    """The four pre-expansion budgets for one parse."""

    max_seconds: float = DEFAULT_PARSE_TIMEOUT_SECONDS
    max_memory_bytes: int = DEFAULT_PARSE_MAX_MEMORY_MB * 1024 * 1024
    max_expanded_chars: int = DEFAULT_PARSE_MAX_EXPANDED_CHARS
    max_output_elements: int = DEFAULT_PARSE_MAX_OUTPUT_ELEMENTS


def budgets_from_env(
    environ: dict[str, str] | None = None, *, max_output_elements: int | None = None
) -> ParseBudgets:
    """Read the budgets from the environment (worker config, no migration)."""
    env = os.environ if environ is None else environ
    return ParseBudgets(
        max_seconds=_positive_float(
            env.get("INGEST_PARSE_TIMEOUT_SECONDS"), DEFAULT_PARSE_TIMEOUT_SECONDS
        ),
        max_memory_bytes=_positive_int(
            env.get("INGEST_PARSE_MAX_MEMORY_MB"), DEFAULT_PARSE_MAX_MEMORY_MB
        )
        * 1024
        * 1024,
        max_expanded_chars=_positive_int(
            env.get("INGEST_PARSE_MAX_EXPANDED_CHARS"), DEFAULT_PARSE_MAX_EXPANDED_CHARS
        ),
        max_output_elements=_positive_int(
            env.get("INGEST_PARSE_MAX_OUTPUT_ELEMENTS"),
            max_output_elements or DEFAULT_PARSE_MAX_OUTPUT_ELEMENTS,
        ),
    )


#: The guard currently installed in this context (see :func:`active_guard`).
#: Context-local on purpose: two parses in one process never share accounting.
_ACTIVE_GUARD: ContextVar[ParseBudgetGuard | None] = ContextVar(
    "ingestion_parse_budget_guard", default=None
)


def active_guard() -> ParseBudgetGuard | None:
    """The budget guard for the parse running in this context, if any."""
    return _ACTIVE_GUARD.get()


def rss_bytes() -> int | None:
    """Best-effort resident set size; ``None`` when the platform cannot say."""
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            rss_pages = int(handle.read().split()[1])
        return rss_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError):
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux reports kilobytes, macOS bytes.
            return int(usage) * (1024 if os.uname().sysname != "Darwin" else 1)
        except Exception:  # pragma: no cover - platform without rusage
            return None


class ParseBudgetGuard:
    """Accounting for one parse; raises :class:`ParseBudgetExceeded` on breach.

    Thread-safe: the memory sampler reads the counters from its own thread
    while the parse thread updates them. A guard is single-use — one guard
    wraps one parse.
    """

    def __init__(self, budgets: ParseBudgets) -> None:
        self.budgets = budgets
        self.expanded_chars = 0
        self.output_elements = 0
        self.peak_rss_bytes = 0
        self.violation: tuple[str, str] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sampler: threading.Thread | None = None
        self._started_at = 0.0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Begin accounting: baseline RSS, deadline, sampler thread."""
        if self._sampler is not None:
            return
        self._started_at = time.monotonic()
        baseline = rss_bytes()
        if baseline:
            self.peak_rss_bytes = baseline
        self._sampler = threading.Thread(
            target=self._sample, name="parse-budget-rss", daemon=True
        )
        self._sampler.start()

    def close(self) -> None:
        """Stop the sampler; safe to call twice."""
        self._stop.set()
        sampler = self._sampler
        if sampler is not None and sampler.is_alive():
            sampler.join(timeout=0.5)
        self._sample_once()

    def __enter__(self) -> ParseBudgetGuard:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- accounting --------------------------------------------------------

    @property
    def elapsed_seconds(self) -> float:
        if not self._started_at:
            return 0.0
        return time.monotonic() - self._started_at

    def observe(self, text: str) -> None:
        """Account one element **as it is produced** (the pre-expansion hook).

        Called from ``ingestion.normalize.make_element``, so every adapter's
        element loop checkpoints here. Raises as soon as any budget is past —
        including a budget tripped by an earlier checkpoint or by an
        abandoned parse, which is what stops a runaway thread.
        """
        if not self._started_at:
            self.start()
        self.output_elements += 1
        self.expanded_chars += len(text)
        self.check()

    def check(self) -> None:
        """Raise if any budget is exhausted (idempotent once tripped)."""
        with self._lock:
            violation = self.violation
        if violation is not None:
            raise ParseBudgetExceeded(*violation)
        budgets = self.budgets
        if self.elapsed_seconds > budgets.max_seconds:
            self.trip(
                TIME_BUDGET,
                f"parse exceeded the {budgets.max_seconds:g}s time budget "
                f"({self.elapsed_seconds:.1f}s elapsed) before finishing expansion",
            )
        if budgets.max_memory_bytes and self.peak_rss_bytes > budgets.max_memory_bytes:
            self.trip(
                MEMORY_BUDGET,
                f"parse exceeded the {budgets.max_memory_bytes // (1024 * 1024)}MiB "
                f"memory budget ({self.peak_rss_bytes // (1024 * 1024)}MiB peak RSS)",
            )
        if self.expanded_chars > budgets.max_expanded_chars:
            self.trip(
                EXPANDED_TEXT_BUDGET,
                f"parse exceeded the {budgets.max_expanded_chars} character "
                f"expanded-text budget ({self.expanded_chars} chars produced)",
            )
        if self.output_elements > budgets.max_output_elements:
            self.trip(
                OUTPUT_BUDGET,
                f"parse exceeded the {budgets.max_output_elements} element output "
                f"budget ({self.output_elements} elements produced)",
            )

    def record_trip(self, code: str, message: str) -> None:
        """Record the breach (sticky) so every later checkpoint raises it."""
        with self._lock:
            if self.violation is None:
                self.violation = (code, message)

    def trip(self, code: str, message: str) -> None:
        """Record the breach (sticky) and raise it at every later checkpoint."""
        self.record_trip(code, message)
        raise ParseBudgetExceeded(code, message)

    def assert_within_budget(self) -> None:
        """Post-parse backstop: accounting is final, re-check it."""
        self._sample_once()
        self.check()

    def snapshot(self) -> dict[str, float | int]:
        """Metrics for the stage logs / job disclosure."""
        return {
            "expanded_chars": self.expanded_chars,
            "output_elements": self.output_elements,
            "peak_rss_bytes": self.peak_rss_bytes,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "budget_seconds": self.budgets.max_seconds,
            "budget_memory_bytes": self.budgets.max_memory_bytes,
            "budget_expanded_chars": self.budgets.max_expanded_chars,
            "budget_output_elements": self.budgets.max_output_elements,
        }

    # -- memory sampler ----------------------------------------------------

    def _sample(self) -> None:
        while not self._stop.wait(0.05):
            self._sample_once()

    def _sample_once(self) -> None:
        current = rss_bytes()
        if current is None:
            return
        with self._lock:
            if current > self.peak_rss_bytes:
                self.peak_rss_bytes = current


def _run_guarded(
    guard: ParseBudgetGuard, coro: Coroutine[Any, Any, ParsedDocument]
) -> ParsedDocument:
    """Run one parse in a worker thread with the guard bound to that context."""
    token = _ACTIVE_GUARD.set(guard)
    try:
        return asyncio.run(coro)
    finally:
        _ACTIVE_GUARD.reset(token)


async def parse_with_budgets(
    parser: DocumentParser,
    data: bytes,
    *,
    mime_type: str,
    filename: str | None = None,
    metadata: dict[str, Any] | None = None,
    budgets: ParseBudgets | None = None,
) -> ParsedDocument:
    """Run ``parser.parse`` under the four budgets, in a bounded worker thread.

    Returns the parser's :class:`~ingestion.interfaces.ParsedDocument`.
    Raises :class:`ParseBudgetExceeded` (never a generic error) on breach.
    """
    effective = budgets or budgets_from_env()
    guard = ParseBudgetGuard(effective)
    guard.start()
    call = parser.parse(data, mime_type=mime_type, filename=filename, metadata=metadata)
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_POOL, _run_guarded, guard, call)
    try:
        parsed = await asyncio.wait_for(future, timeout=effective.max_seconds)
    except TimeoutError as exc:
        # The worker thread is abandoned, not killed: the guard is now tripped,
        # so it aborts at its next expansion checkpoint (module docstring).
        message = (
            f"parse exceeded the {effective.max_seconds:g}s time budget and was "
            "abandoned; a parser blocked inside a third-party call is not "
            "killable from Python and keeps its thread until it returns"
        )
        guard.record_trip(TIME_BUDGET, message)
        raise ParseBudgetExceeded(TIME_BUDGET, message) from exc
    finally:
        guard.close()
    guard.assert_within_budget()
    return parsed
