"""W7 fault-injection matrix — harness + stated table (T-V4-025).

This module is TEST-ONLY infrastructure: it contains no product logic and
changes nothing about how the executor runs.  Every fault below is injected
through a seam that already exists (``NodeExecutor`` injection, the injectable
retry ``sleep``, the injectable run ``clock``, the injectable checkpoint
store, the idempotency ledger and ``request_cancel``) — the matrix is the
proof that those seams are sufficient, which is why no fault hooks were added
to the executor.

THE MATRIX (fault → layer → honest terminal state + attribution):

=========================  ====================  ==========  ==========================
fault                     layer                 terminal    attribution asserted
=========================  ====================  ==========  ==========================
node_crash_mid_run         node                  FAILED      run.failed_node_id names
                                                             the node; error carries the
                                                             node's message
runner_timeout             node (per-node bound) FAILED      error names the per-node
                                                             timeout bound + elapsed
cancel_during_retry        retry                 CANCELLED   error names cancellation;
                                                             no COMPLETED-after-cancel
double_dispatch            delivery              COMPLETED   exactly one effect per node
                                                             (ledger), never two
checkpoint_write_failure   store                 own terminal + ``checkpoint_failures``
                                                             >= 1 DISCLOSED (never
                                                             silent; this is the only
                                                             cell whose run may end
                                                             COMPLETED — with the
                                                             fault counted, not hidden)
clock_skew_forward         envelope (deadline)   CANCELLED   error names "run deadline
                                                             reached" + bound
clock_skew_backward        envelope (deadline)   own terminal run finishes honestly;
                                                             elapsed never negative,
                                                             never hung
deadline_already_spent     envelope (deadline)   CANCELLED   no node executes at all
=========================  ====================  ==========  ==========================

Every cell additionally asserts: the run FINISHED (no hang — an explicit
elapsed ceiling), every node record is terminal (no RUNNING/PENDING residue)
and no run reports COMPLETED while a fault is inside it undisclosed.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from .checkpoint import CheckpointStore
from .interfaces import NodeSpec, NodeType

#: Elapsed ceiling for every matrix cell (faults must resolve fast).
FAULT_RESOLUTION_LIMIT_S = 5.0


class ScriptedNodeExecutor:
    """NodeExecutor whose behaviour is scripted per node (injected seam)."""

    def __init__(self, behaviours: dict[str, Any] | None = None) -> None:
        self._behaviours = behaviours or {}
        self.executes: dict[str, int] = {}

    async def execute_node(
        self,
        node: NodeSpec,
        input_data: dict[str, Any] | Any,
        workspace_id: str,
    ) -> dict[str, Any]:
        self.executes[node.id] = self.executes.get(node.id, 0) + 1
        behaviour = self._behaviours.get(node.id, "ok")
        if behaviour == "raise":
            raise RuntimeError(f"node {node.id} crashed")
        if behaviour == "always-fail":
            raise RuntimeError(f"node {node.id} transient failure")
        if behaviour == "hang":
            import asyncio

            await asyncio.sleep(30)
        if node.type is NodeType.TRIGGER:
            return {"output": input_data}
        return {"output": f"{node.id}-out"}


class SkewClock:
    """Injectable clock that jumps by a fixed offset on a chosen call (seam:
    ``configure_run_bounds(clock=...)``)."""

    def __init__(self, jump_after_calls: int, jump_s: float, start: float | None = None) -> None:
        self._calls = 0
        self._jump_after = jump_after_calls
        self._jump = jump_s
        self._now = start if start is not None else time.monotonic()

    def __call__(self) -> float:
        self._calls += 1
        if self._calls == self._jump_after:
            self._now += self._jump
        return self._now


class FailingCheckpointStore(CheckpointStore):
    """Checkpoint store whose writes always fail (injected seam)."""

    def __init__(self) -> None:
        self.attempts = 0

    async def append(self, checkpoint: Any) -> Any:
        self.attempts += 1
        raise RuntimeError("checkpoint store unavailable")

    async def latest(self, run_id: str) -> Any:
        return None

    async def evidence_for(self, run_id: str) -> list:
        return []

    async def next_sequence(self, run_id: str) -> int:
        return 1


def assert_run_finished(
    run: Any,
    *,
    elapsed_s: float,
    limit_s: float = FAULT_RESOLUTION_LIMIT_S,
) -> None:
    """Shared cell assertion: finished fast, records terminal, no residue."""
    assert elapsed_s < limit_s, f"run did not resolve within {limit_s}s (hung?)"
    assert run.finished_at, "a finished run records finished_at"
    terminal = {"completed", "failed", "cancelled"}
    assert run.status.value in terminal, f"non-terminal run status {run.status}"
    for node_id, record in run.node_runs.items():
        assert record.status.value in {"completed", "failed", "skipped"}, (
            f"node {node_id} left in {record.status.value} after the run ended"
        )


def now_utc() -> datetime:
    return datetime.now(UTC)
