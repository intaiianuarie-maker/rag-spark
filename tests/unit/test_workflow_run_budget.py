"""T-V4-012 — run-level budgets + deadlines (the outer envelope).

Envelope table (stated in ``workflow_engine.executor`` with defaults):

* ``RUN_DEADLINE_S = 300.0``        — run-wide wall-clock; expiry cancels
  cleanly to CANCELLED with the deadline named, never a hung run.
* ``RUN_MAX_NODE_SECONDS = 600.0``  — total node execution time; exhaustion
  fails loudly naming the spent value, never silent truncation.
* ``RUN_MAX_NODES = 200``           — executed-node cap (the accounting-side
  assertion against the validator's 200-node graph cap).

COMPOSITION RULE with the T-V4-010 per-node bounds: the deadline is measured
as WALL-CLOCK ELAPSED, the resource budget as NODE-SECONDS SPENT, and each
node's work is charged exactly ONCE (attempts + bounded backoff).  The
effective per-node timeout is shortened to the deadline's remaining time, so
a node cannot outlive the run and a node timeout never consumes the run
deadline twice.  The envelope outranks the node's own outcome: once the
deadline or budget is spent, the run ends on the envelope (CANCELLED /
FAILED) with the bound named.

Both metrics: runs escaping their envelope: 0 (over-deadline cancels with
the deadline named, over-budget fails with the budget named) and legitimate
runs killed: 0 (just-under-envelope completes; composition proven).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from workflow_engine import (
    NodeRunStatus,
    NodeSpec,
    NodeType,
    PortSpec,
    WorkflowCompiler,
    WorkflowDefinition,
    WorkflowEdgeSpec,
    WorkflowExecutor,
    WorkflowRevision,
    WorkflowRunStatus,
)
from workflow_engine.executor import (
    RUN_DEADLINE_S,
    RUN_MAX_NODE_SECONDS,
    RUN_MAX_NODES,
)

#: Wall-clock ceiling for the "refused fast" proofs.
REFUSAL_TIME_LIMIT_S = 5.0


def _node(
    node_id: str,
    node_type: NodeType,
    *,
    in_ports: tuple[str, ...] = ("input",),
    out_ports: tuple[str, ...] = ("output",),
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        type=node_type,
        input_ports=[PortSpec(name=p, type="any") for p in in_ports],
        output_ports=[PortSpec(name=p, type="any") for p in out_ports],
    )


def _edge(edge_id: str, source: str, target: str) -> WorkflowEdgeSpec:
    return WorkflowEdgeSpec(
        id=edge_id,
        source_node_id=source,
        source_port="output",
        target_node_id=target,
        target_port="input",
    )


class _Scripted:
    """NodeExecutor with per-node behaviour and attempt accounting."""

    def __init__(self, behaviours: dict[str, Any], sleeps: dict[str, float] | None = None) -> None:
        self._behaviours = behaviours
        self._sleeps = sleeps or {}
        self.attempts: dict[str, int] = {}

    async def execute_node(
        self,
        node: NodeSpec,
        input_data: dict[str, Any] | Any,
        workspace_id: str,
    ) -> dict[str, Any]:
        self.attempts[node.id] = self.attempts.get(node.id, 0) + 1
        if node.id in self._sleeps:
            await asyncio.sleep(self._sleeps[node.id])
        behaviour = self._behaviours.get(node.id, "ok")
        if behaviour == "always-fail":
            raise RuntimeError("boom")
        if node.type is NodeType.TRIGGER:
            return {"output": input_data}
        return {"output": f"{node.id}-out"}


def _compile(nodes: list[NodeSpec], edges: list[WorkflowEdgeSpec]) -> tuple:
    spec = WorkflowDefinition(
        id="wf-run-env", workspace_id="ws-1", name="RunEnv",
        nodes=nodes, edges=edges,
    )
    revision = WorkflowRevision.create(spec, revision=1)
    return WorkflowCompiler.compile(revision), revision


def _linear(**behaviours: Any) -> tuple:
    nodes = [
        _node("start", NodeType.TRIGGER, in_ports=()),
        _node("mid", NodeType.TRANSFORM),
        _node("end", NodeType.OUTPUT),
    ]
    edges = [_edge("e1", "start", "mid"), _edge("e2", "mid", "end")]
    plan, revision = _compile(nodes, edges)
    return plan, revision, behaviours


async def _run(
    plan,
    revision,
    behaviours: dict[str, Any],
    *,
    sleeps: dict[str, float] | None = None,
    configure=None,
    initial: dict | None = None,
) -> tuple:
    fake = _Scripted(behaviours, sleeps)
    executor = WorkflowExecutor(fake)
    if configure is not None:
        configure(executor)
    started = time.perf_counter()
    run = await executor.execute(plan, revision, initial or {"input": "x"})
    elapsed = time.perf_counter() - started
    return run, fake, executor, elapsed


# ---------------------------------------------------------------------------
# Deadline — over-deadline cancels with the deadline named
# ---------------------------------------------------------------------------


class TestRunDeadline:
    async def test_over_deadline_run_cancels_naming_the_deadline(self) -> None:
        plan, revision, behaviours = _linear(mid="ok")
        run, _fake, executor, elapsed = await _run(
            plan, revision, behaviours,
            sleeps={"mid": 0.3},
            configure=lambda ex: ex.configure_run_bounds(deadline_s=0.05),
        )
        assert run.status == WorkflowRunStatus.CANCELLED
        assert "run deadline reached" in (run.error or "")
        assert "0.05" in (run.error or "")  # the bound is named
        assert "elapsed" in (run.error or "")  # the observed value is named
        # Composition: the node cannot outlive the run — its effective
        # timeout was shortened to the remaining run time, so it failed with
        # the adjusted bound named; the remaining node is skipped/disclosed.
        assert run.node_runs["mid"].status == NodeRunStatus.FAILED
        assert "run-deadline-adjusted per-node timeout" in (run.node_runs["mid"].error or "")
        assert run.node_runs["end"].status == NodeRunStatus.SKIPPED
        assert "deadline" in (run.node_runs["end"].error or "").lower()
        # Refused fast — no hang.
        assert elapsed < REFUSAL_TIME_LIMIT_S
        # Accounting recorded.
        assert executor.last_envelope is not None
        assert executor.last_envelope.nodes_executed >= 2

    async def test_deadline_cancels_before_dispatch_when_already_spent(self) -> None:
        """A run whose budget is spent before the next node never starts it."""
        plan, revision, behaviours = _linear(mid="ok", end="always-fail")
        fake = _Scripted(behaviours, {"mid": 0.2})
        executor = WorkflowExecutor(fake)
        executor.configure_run_bounds(deadline_s=0.1)
        run = await executor.execute(plan, revision, {"input": "x"})
        assert run.status == WorkflowRunStatus.CANCELLED
        # `end` never started — the failing behaviour was never reached.
        assert fake.attempts.get("end") is None
        assert run.node_runs["end"].status == NodeRunStatus.SKIPPED

    async def test_just_under_deadline_completes(self) -> None:
        """Legitimate runs killed: 0 — just-under-envelope completes."""
        plan, revision, behaviours = _linear()
        run, _, _, elapsed = await _run(
            plan, revision, behaviours,
            configure=lambda ex: ex.configure_run_bounds(deadline_s=30.0),
        )
        assert run.status == WorkflowRunStatus.COMPLETED
        assert run.final_output == "end-out"
        assert elapsed < REFUSAL_TIME_LIMIT_S

    def test_deadline_default_stated(self) -> None:
        assert RUN_DEADLINE_S == 300.0


# ---------------------------------------------------------------------------
# Resource budgets — over-budget fails with the budget named
# ---------------------------------------------------------------------------


class TestRunBudgets:
    async def test_node_seconds_budget_exhaustion_fails_named(self) -> None:
        plan, revision, behaviours = _linear()
        run, _fake, _, elapsed = await _run(
            plan, revision, behaviours,
            sleeps={"mid": 0.05},
            configure=lambda ex: ex.configure_run_bounds(max_node_seconds=0.01),
        )
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id in {"start", "mid"}
        assert "node-seconds budget exhausted" in (run.error or "")
        assert "budget 0.01s" in (run.error or "")
        assert "spent" in (run.error or "")  # the observed value is named
        assert elapsed < REFUSAL_TIME_LIMIT_S

    async def test_node_count_budget_exhaustion_fails_named(self) -> None:
        plan, revision, behaviours = _linear()
        run, _, _, _ = await _run(
            plan, revision, behaviours,
            configure=lambda ex: ex.configure_run_bounds(max_nodes=1),
        )
        assert run.status == WorkflowRunStatus.FAILED
        assert "node-count budget exhausted" in (run.error or "")
        assert "budget 1" in (run.error or "")
        assert "2 nodes executed" in (run.error or "")

    async def test_just_under_budgets_completes(self) -> None:
        plan, revision, behaviours = _linear()
        run, _, executor, _ = await _run(
            plan, revision, behaviours,
            configure=lambda ex: ex.configure_run_bounds(
                max_node_seconds=30.0, max_nodes=50
            ),
        )
        assert run.status == WorkflowRunStatus.COMPLETED
        assert executor.last_envelope is not None
        assert executor.last_envelope.nodes_executed == 3
        assert executor.last_envelope.node_seconds < 30.0

    def test_budget_defaults_stated(self) -> None:
        assert RUN_MAX_NODE_SECONDS == 600.0
        assert RUN_MAX_NODES == 200


# ---------------------------------------------------------------------------
# Composition rule — node bounds are the inner layer, charged once
# ---------------------------------------------------------------------------


class TestCompositionRule:
    async def test_node_timeout_inside_generous_envelope_charges_once(self) -> None:
        """A node that times out (inner bound) inside a generous run envelope
        (outer bounds) fails the NODE; the run then fails per the terminal
        table.  The run deadline is wall-clock elapsed (not node-seconds), so
        the timeout is charged once, never twice, and the run does not hang
        for the node's full sleep."""
        plan, revision, behaviours = _linear(mid="hang")
        run, fake, executor, elapsed = await _run(
            plan, revision, behaviours,
            sleeps={"mid": 5.0},
            configure=lambda ex: (
                ex.configure_bounds(node_timeout_s=0.05),
                ex.configure_run_bounds(
                    deadline_s=30.0, max_node_seconds=30.0
                ),
            ),
        )
        # The node failed on ITS bound, and the run failed naming it.
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id == "mid"
        assert "per-node timeout" in (run.error or "")
        # One charge: node-seconds ~ the timeout, not the 5 s sleep.
        envelope = executor.last_envelope
        assert envelope is not None
        # The trigger ran once and `mid` was charged ONCE (its 3 attempts
        # inside one outcome): node-seconds ~0.3s, never 3x the timeout and
        # never the 5s sleep.
        assert envelope.nodes_executed == 2
        assert envelope.node_seconds < 1.0
        # Wall-clock bounded by the node timeout, not the node sleep.
        assert elapsed < REFUSAL_TIME_LIMIT_S
        assert fake.attempts["mid"] == 3  # retries still capped (T-V4-010)

    async def test_node_timeout_adjusted_to_remaining_run_time(self) -> None:
        """When the deadline binds before the node's own timeout, the message
        says so — the node cannot outlive the run."""
        plan, revision, behaviours = _linear(mid="hang")
        run, _, _, elapsed = await _run(
            plan, revision, behaviours,
            sleeps={"mid": 5.0},
            configure=lambda ex: (
                ex.configure_bounds(node_timeout_s=30.0),
                ex.configure_run_bounds(deadline_s=0.05),
            ),
        )
        # The run ends on its envelope with the deadline named.
        assert run.status == WorkflowRunStatus.CANCELLED
        assert "run deadline reached" in (run.error or "")
        assert elapsed < REFUSAL_TIME_LIMIT_S

    async def test_node_cannot_outlive_the_run_deadline(self) -> None:
        """Composition rule, stated: a node's effective timeout is the run
        deadline's remaining time, so a long node fails on the ADJUSTED bound
        and the run ends CANCELLED — the node never outlives the envelope
        and the deadline is never double-counted."""
        plan, revision, behaviours = _linear()
        run, _, _, elapsed = await _run(
            plan, revision, behaviours,
            sleeps={"mid": 0.2},
            configure=lambda ex: ex.configure_run_bounds(deadline_s=0.05),
        )
        assert run.status == WorkflowRunStatus.CANCELLED
        assert "run deadline reached" in (run.error or "")
        assert run.node_runs["mid"].status == NodeRunStatus.FAILED
        assert "run-deadline-adjusted" in (run.node_runs["mid"].error or "")
        assert elapsed < REFUSAL_TIME_LIMIT_S


# ---------------------------------------------------------------------------
# Configuration hygiene
# ---------------------------------------------------------------------------


class TestEnvelopeConfiguration:
    def test_invalid_envelope_rejected_at_wiring_time(self) -> None:
        executor = WorkflowExecutor(_Scripted({}))
        cases: tuple[dict[str, Any], ...] = (
            {"deadline_s": 0},
            {"deadline_s": -1},
            {"max_node_seconds": 0},
            {"max_nodes": 0},
        )
        for case in cases:
            with pytest.raises(ValueError):
                executor.configure_run_bounds(**case)

    def test_defaults_active_for_plain_construction(self) -> None:
        plan, revision, behaviours = _linear()
        run, _fake, executor = asyncio.run(_run_plain(plan, revision, behaviours))
        assert run.status == WorkflowRunStatus.COMPLETED
        envelope = executor.last_envelope
        assert envelope is not None
        assert envelope.deadline_s == RUN_DEADLINE_S
        assert envelope.max_node_seconds == RUN_MAX_NODE_SECONDS
        assert envelope.max_nodes == RUN_MAX_NODES


async def _run_plain(plan, revision, behaviours):
    fake = _Scripted(behaviours)
    executor = WorkflowExecutor(fake)
    run = await executor.execute(plan, revision, {"input": "x"})
    return run, fake, executor
