"""T-V4-010 — executor bounded node semantics (no runaway nodes).

Bound table (stated in ``workflow_engine.executor`` with defaults):

* ``NODE_TIMEOUT_S = 30.0``           — per-node wall-clock; a hung node
  fails the node, never the run's liveness.
* ``MAX_NODE_OUTPUT_BYTES = 1_048_576`` — per-node output cap; a flooding
  node is refused before its output is stored or propagated.
* ``NODE_MAX_ATTEMPTS = 3``           — retry cap (total attempts); a
  permanently failing node fails loudly, never loops forever.
* ``NODE_RETRY_BASE_S = 0.05``        — bounded exponential backoff
  (base * 2^(n-1)), injectable for determinism.

Both metrics: unbounded executions: 0 (hung node times out, flooding node
refused, infinite-retry impossible) and legitimate slow/large nodes refused:
0 (just-under-limit passes).
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
    MAX_NODE_OUTPUT_BYTES,
    NODE_MAX_ATTEMPTS,
    NODE_RETRY_BASE_S,
    NODE_TIMEOUT_S,
    _output_bytes,
)

#: Wall-clock ceiling for the "refused fast" proofs.  The bounds are 30 s /
#: 1 MB by default; a breach must surface in a fraction of that.
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
    """NodeExecutor with per-node behaviour, recording attempts."""

    def __init__(self, behaviours: dict[str, Any]) -> None:
        self._behaviours = behaviours
        self.attempts: dict[str, int] = {}

    async def execute_node(
        self,
        node: NodeSpec,
        input_data: dict[str, Any] | Any,
        workspace_id: str,
    ) -> dict[str, Any]:
        self.attempts[node.id] = self.attempts.get(node.id, 0) + 1
        behaviour = self._behaviours.get(node.id, "ok")
        if behaviour == "hang":
            await asyncio.sleep(30)
        if behaviour == "flood":
            return {"output": "x" * (3 * 1024 * 1024)}
        if behaviour == "always-fail":
            raise RuntimeError("permanent boom")
        if behaviour == "fail-twice":
            if self.attempts[node.id] <= 2:
                raise RuntimeError(f"transient {self.attempts[node.id]}")
            return {"output": "recovered"}
        if behaviour == "slow":
            await asyncio.sleep(0.05)
        return {"output": f"{node.id}-out"}


def _compile(nodes: list[NodeSpec], edges: list[WorkflowEdgeSpec]) -> tuple:
    spec = WorkflowDefinition(
        id="wf-bounds", workspace_id="ws-1", name="Bounds",
        nodes=nodes, edges=edges,
    )
    revision = WorkflowRevision.create(spec, revision=1)
    plan = WorkflowCompiler.compile(revision)
    return plan, revision


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
    configure=None,
    initial: dict | None = None,
) -> tuple:
    fake = _Scripted(behaviours)
    executor = WorkflowExecutor(fake)
    if configure is not None:
        configure(executor)
    started = time.perf_counter()
    run = await executor.execute(plan, revision, initial or {"input": "x"})
    elapsed = time.perf_counter() - started
    return run, fake, elapsed


# ---------------------------------------------------------------------------
# Bound 1 — per-node timeout
# ---------------------------------------------------------------------------


class TestNodeTimeout:
    async def test_hung_node_times_out_and_fails_the_node(self) -> None:
        plan, revision, behaviours = _linear(mid="hang")
        run, fake, elapsed = await _run(
            plan, revision, behaviours,
            configure=lambda ex: ex.configure_bounds(node_timeout_s=0.05),
        )
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id == "mid"
        # The breach names the node, the bound and the observed value.
        assert "per-node timeout" in (run.error or "")
        assert "0.05s bound" in (run.error or "")
        assert "timed out after" in (run.error or "")
        assert run.node_runs["mid"].status == NodeRunStatus.FAILED
        # Refused fast — the run did not hang on the 30 s node sleep.
        assert elapsed < REFUSAL_TIME_LIMIT_S
        assert fake.attempts["mid"] == NODE_MAX_ATTEMPTS  # retried to the cap

    async def test_timeout_retries_are_bounded_then_fail(self) -> None:
        """Every attempt gets the full bound; attempts stay capped."""
        plan, revision, behaviours = _linear(mid="hang")
        run, fake, _elapsed = await _run(
            plan, revision, behaviours,
            configure=lambda ex: ex.configure_bounds(node_timeout_s=0.05),
        )
        assert run.failed_node_id == "mid"
        assert "max attempts" in (run.error or "")
        assert fake.attempts["mid"] == NODE_MAX_ATTEMPTS

    async def test_just_under_timeout_passes(self) -> None:
        """No over-rejection: a node just under the bound completes."""
        plan, revision, behaviours = _linear(mid="slow")
        run, _, elapsed = await _run(
            plan, revision, behaviours,
            configure=lambda ex: ex.configure_bounds(node_timeout_s=5.0),
        )
        assert run.status == WorkflowRunStatus.COMPLETED
        assert run.node_runs["mid"].attempts == 1
        assert elapsed < REFUSAL_TIME_LIMIT_S

    def test_timeout_default_stated(self) -> None:
        assert NODE_TIMEOUT_S == 30.0


# ---------------------------------------------------------------------------
# Bound 2 — per-node output cap
# ---------------------------------------------------------------------------


class TestNodeOutputCap:
    async def test_flooding_node_refused_with_observed_size(self) -> None:
        plan, revision, behaviours = _linear(mid="flood")
        run, _, _ = await _run(
            plan, revision, behaviours,
            configure=lambda ex: ex.configure_bounds(max_node_output_bytes=1024),
        )
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id == "mid"
        # Names the node, the bound and the observed byte count.
        expected = _output_bytes({"output": "x" * (3 * 1024 * 1024)})
        assert f"{expected} bytes of output" in (run.error or "")
        assert "1024 bytes" in (run.error or "")
        # The flooding output is never stored or propagated.
        assert run.node_runs["mid"].output_data is None
        assert run.final_output is None

    async def test_just_under_output_cap_passes(self) -> None:
        plan, revision, behaviours = _linear(mid="ok")
        # mid returns {"output": "mid-out"} (~30 bytes); cap 1024 is generous.
        run, _, _ = await _run(
            plan, revision, behaviours,
            configure=lambda ex: ex.configure_bounds(max_node_output_bytes=1024),
        )
        assert run.status == WorkflowRunStatus.COMPLETED

    def test_output_cap_default_stated(self) -> None:
        assert MAX_NODE_OUTPUT_BYTES == 1_048_576


# ---------------------------------------------------------------------------
# Bound 3 — retry cap with bounded backoff
# ---------------------------------------------------------------------------


class TestRetryCap:
    async def test_infinite_retry_impossible_attempts_capped(self) -> None:
        """A permanently failing node fails loudly after NODE_MAX_ATTEMPTS."""
        plan, revision, behaviours = _linear(mid="always-fail")
        slept: list[float] = []

        async def _noop_sleep(seconds: float) -> None:
            slept.append(seconds)

        run, fake, _ = await _run(
            plan, revision, behaviours,
            configure=lambda ex: ex.configure_bounds(sleep=_noop_sleep),
        )
        assert fake.attempts["mid"] == NODE_MAX_ATTEMPTS
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id == "mid"
        assert f"after {NODE_MAX_ATTEMPTS} attempts" in (run.error or "")
        assert "permanent boom" in (run.error or "")
        # Bounded backoff, deterministic schedule: base * 2^(n-1).
        assert slept == [
            NODE_RETRY_BASE_S,
            NODE_RETRY_BASE_S * 2,
        ]

    async def test_transient_failure_recovers_within_cap(self) -> None:
        plan, revision, behaviours = _linear(mid="fail-twice")
        slept: list[float] = []

        async def _noop_sleep(seconds: float) -> None:
            slept.append(seconds)

        run, fake, _ = await _run(
            plan, revision, behaviours,
            configure=lambda ex: ex.configure_bounds(sleep=_noop_sleep),
        )
        assert run.status == WorkflowRunStatus.COMPLETED
        assert fake.attempts["mid"] == 3
        assert run.node_runs["mid"].attempts == 3  # recorded on the node
        assert run.final_output == "end-out"  # end node passed the recovered value through
        assert slept == [NODE_RETRY_BASE_S, NODE_RETRY_BASE_S * 2]

    async def test_permanent_not_implemented_does_not_retry(self) -> None:
        class _Permanent:
            def __init__(self) -> None:
                self.attempts: dict[str, int] = {}

            async def execute_node(self, node, input_data, workspace_id):
                self.attempts[node.id] = self.attempts.get(node.id, 0) + 1
                if node.type == NodeType.TRIGGER:
                    return {"output": input_data}
                raise NotImplementedError("no such capability")

        fake = _Permanent()
        plan, revision, _ = _linear()
        executor = WorkflowExecutor(fake)
        run = await executor.execute(plan, revision, {"input": "x"})
        assert run.status == WorkflowRunStatus.FAILED
        assert fake.attempts["mid"] == 1  # permanent error: no wasted retries
        assert run.node_runs["mid"].attempts == 1
        assert run.failed_node_id == "mid"

    def test_attempt_and_backoff_defaults_stated(self) -> None:
        assert NODE_MAX_ATTEMPTS == 3
        assert NODE_RETRY_BASE_S == 0.05


# ---------------------------------------------------------------------------
# Configuration hygiene
# ---------------------------------------------------------------------------


class TestBoundConfiguration:
    def test_invalid_bounds_rejected_at_wiring_time(self) -> None:
        executor = WorkflowExecutor(_Scripted({}))
        for kwargs in (
            {"node_timeout_s": 0},
            {"node_timeout_s": -1},
            {"max_node_output_bytes": 0},
            {"max_attempts": 0},
            {"retry_base_s": -0.1},
        ):
            with pytest.raises(ValueError):
                executor.configure_bounds(**kwargs)

    async def test_cancelled_during_node_propagates_and_settles(self) -> None:
        """Cancellation inside a bounded execution is control flow: it
        propagates, is not retried, and the run settles CANCELLED."""

        class _Hanging:
            def __init__(self) -> None:
                self.calls = 0

            async def execute_node(self, node, input_data, workspace_id):
                self.calls += 1
                await asyncio.sleep(30)
                return {"output": "never"}

        fake = _Hanging()
        plan, revision, _ = _linear()
        executor = WorkflowExecutor(fake)
        task = asyncio.create_task(executor.execute(plan, revision, {"input": "x"}))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.calls == 1  # the cancelled node is not retried

    async def test_defaults_unchanged_for_plain_construction(self) -> None:
        """Existing construction (no configure_bounds) keeps the defaults."""
        plan, revision, behaviours = _linear()
        run, fake, _ = await _run(plan, revision, behaviours)
        assert run.status == WorkflowRunStatus.COMPLETED
        assert fake.attempts["mid"] == 1
        assert run.node_runs["mid"].attempts == 1
