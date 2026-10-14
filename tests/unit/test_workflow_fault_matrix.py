"""T-V4-025 — W7 fault-injection matrix (zero false-completes under faults).

Every fault is injected through an EXISTING seam (the matrix lives in
``workflow_engine.fault_matrix``; no executor/validator/compiler logic is
touched).  Each cell asserts the honest terminal state, the fault attribution,
no hang, and no non-terminal residue.

The table (fault → layer → terminal):

* ``node_crash_mid_run``       node          → FAILED (node named)
* ``runner_timeout``           node bound    → FAILED (timeout bound + elapsed)
* ``cancel_during_retry``      retry         → CANCELLED (no COMPLETED-after-cancel)
* ``double_dispatch``          delivery      → COMPLETED with exactly ONE effect
* ``checkpoint_write_failure`` store         → own terminal + ``checkpoint_failures``
                                               disclosed (never silent)
* ``clock_skew_forward``       deadline      → CANCELLED (deadline named)
* ``clock_skew_backward``      deadline      → own terminal, honest, not hung
* ``deadline_already_spent``   deadline      → CANCELLED, zero nodes executed
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from workflow_engine import (
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
    NODE_TIMEOUT_S,
    RUN_DEADLINE_S,
    RUN_MAX_NODE_SECONDS,
    RUN_MAX_NODES,
)
from workflow_engine.fault_matrix import (
    FAULT_RESOLUTION_LIMIT_S,
    FailingCheckpointStore,
    ScriptedNodeExecutor,
    SkewClock,
    assert_run_finished,
)
from workflow_engine.interfaces import NodeRunStatus


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


def _linear() -> tuple:
    nodes = [
        _node("start", NodeType.TRIGGER, in_ports=()),
        _node("mid", NodeType.TRANSFORM),
        _node("end", NodeType.OUTPUT),
    ]
    edges = [_edge("e1", "start", "mid"), _edge("e2", "mid", "end")]
    spec = WorkflowDefinition(
        id="wf-fault", workspace_id="ws-1", name="Fault",
        nodes=nodes, edges=edges,
    )
    revision = WorkflowRevision.create(spec, revision=1)
    return WorkflowCompiler.compile(revision), revision


async def _drive(
    plan,
    revision,
    behaviours: dict[str, Any],
    *,
    run_id: str | None = None,
    resume: bool = False,
    configure=None,
    initial: dict | None = None,
) -> tuple:
    fake = ScriptedNodeExecutor(behaviours)
    executor = WorkflowExecutor(fake)
    if configure is not None:
        configure(executor)
    started = time.perf_counter()
    run = await executor.execute(
        plan, revision, initial or {"input": "x"}, run_id=run_id, resume=resume
    )
    elapsed = time.perf_counter() - started
    return run, fake, executor, elapsed


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


class TestFaultMatrix:
    async def test_node_crash_mid_run_fails_naming_the_node(self) -> None:
        plan, revision = _linear()
        run, fake, _, elapsed = await _drive(
            plan, revision, {"mid": "raise"}, run_id="f-crash"
        )
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id == "mid"
        assert "mid crashed" in (run.error or "")
        assert run.node_runs["mid"].status == NodeRunStatus.FAILED
        assert run.node_runs["end"].status == NodeRunStatus.SKIPPED  # disclosed
        assert fake.executes["end"] is None if "end" in fake.executes else True
        assert "end" not in fake.executes  # never started after the crash
        assert_run_finished(run, elapsed_s=elapsed)

    async def test_runner_timeout_fails_naming_the_bound(self) -> None:
        plan, revision = _linear()
        run, _, _, elapsed = await _drive(
            plan,
            revision,
            {"mid": "hang"},
            run_id="f-timeout",
            configure=lambda ex: ex.configure_bounds(node_timeout_s=0.05),
        )
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id == "mid"
        assert "per-node timeout" in (run.error or "")
        assert "0.05s bound" in (run.error or "")
        assert "timed out after" in (run.error or "")
        assert_run_finished(run, elapsed_s=elapsed)
        assert elapsed < FAULT_RESOLUTION_LIMIT_S  # the 30 s hang cut to 0.05 s

    async def test_cancel_during_retry_ends_cancelled_not_completed(self) -> None:
        """Cancel lands inside the retry backoff of a node that then
        succeeds: the run ends CANCELLED at the next dispatch boundary —
        never a COMPLETED-after-cancel, and the settled node keeps its
        recorded output (in-flight settled, not re-run)."""
        plan, revision = _linear()

        class _TransientThenOk:
            """`mid` fails once, succeeds on its second attempt."""

            def __init__(self) -> None:
                self.mid_attempts = 0

            async def execute_node(self, node, input_data, workspace_id):
                if node.type is NodeType.TRIGGER:
                    return {"output": input_data}
                self.mid_attempts += 1
                if self.mid_attempts == 1:
                    raise RuntimeError("transient")
                return {"output": f"{node.id}-out"}

        fake = _TransientThenOk()
        executor = WorkflowExecutor(fake)

        async def _cancel_sleep(seconds: float) -> None:
            # The first (and only) backoff is where the cancel arrives.
            executor.request_cancel()

        executor.configure_bounds(
            max_attempts=3, retry_base_s=0.0, sleep=_cancel_sleep
        )
        started = time.perf_counter()
        run = await executor.execute(plan, revision, {"input": "x"}, run_id="f-cancel")
        elapsed = time.perf_counter() - started

        assert run.status == WorkflowRunStatus.CANCELLED
        assert run.error == "run cancelled by request"
        assert run.failed_node_id is None
        # The retried node settled (its output recorded), the next node never ran.
        assert run.node_runs["mid"].status == NodeRunStatus.COMPLETED
        assert run.node_runs["mid"].attempts == 2
        assert run.node_runs["end"].status == NodeRunStatus.SKIPPED
        assert fake.mid_attempts == 2  # one failure, one success; no blind retry loop
        assert_run_finished(run, elapsed_s=elapsed)

    async def test_double_dispatch_produces_one_effect(self) -> None:
        plan, revision = _linear()
        fake = ScriptedNodeExecutor({})
        executor = WorkflowExecutor(fake)
        first = await executor.execute(plan, revision, {"input": "x"}, run_id="f-dup")
        second = await executor.execute(plan, revision, {"input": "x"}, run_id="f-dup")
        assert first.status == WorkflowRunStatus.COMPLETED
        assert second.status == WorkflowRunStatus.COMPLETED
        # Exactly one execution per node across both deliveries.
        assert fake.executes == {"start": 1, "mid": 1, "end": 1}
        assert second.final_output == first.final_output
        assert_run_finished(second, elapsed_s=0.0)

    async def test_checkpoint_write_failure_is_disclosed_not_silent(self) -> None:
        """The only cell whose run may end COMPLETED: the run itself is fine
        and the fault is DISCLOSED (``checkpoint_failures``), never silent."""
        plan, revision = _linear()
        store = FailingCheckpointStore()
        run, _, executor, elapsed = await _drive(
            plan, revision, {}, run_id="f-store",
            configure=lambda ex: ex.configure_checkpoint_store(store),
        )
        assert run.status == WorkflowRunStatus.COMPLETED
        assert store.attempts >= 1  # the writes were attempted
        assert executor.checkpoint_failures >= 1  # and disclosed, not silent
        assert run.final_output == "end-out"
        assert_run_finished(run, elapsed_s=elapsed)

    async def test_clock_skew_forward_cancels_naming_the_deadline(self) -> None:
        """The clock jumps past the deadline mid-run: the run ends CANCELLED
        with the deadline named, no node starts after the jump."""
        plan, revision = _linear()
        # First call anchors; the second (post-start) call jumps 10 s ahead
        # against a 5 s deadline.
        clock = SkewClock(jump_after_calls=2, jump_s=10.0)
        run, fake, _, elapsed = await _drive(
            plan, revision, {}, run_id="f-skew",
            configure=lambda ex: ex.configure_run_bounds(
                deadline_s=5.0, clock=clock
            ),
        )
        assert run.status == WorkflowRunStatus.CANCELLED
        assert "run deadline reached" in (run.error or "")
        assert "5.0s" in (run.error or "")
        assert elapsed < FAULT_RESOLUTION_LIMIT_S
        assert_run_finished(run, elapsed_s=elapsed)
        # No node started after the deadline jump (at most the first node ran).
        assert fake.executes.get("end") is None

    async def test_clock_skew_backward_finishes_honestly(self) -> None:
        """A backward jump must not hang the run or fake elapsed time: the run
        terminates honestly and every node is settled."""
        plan, revision = _linear()
        clock = SkewClock(jump_after_calls=2, jump_s=-3600.0)
        run, fake, executor, elapsed = await _drive(
            plan, revision, {}, run_id="f-back",
            configure=lambda ex: ex.configure_run_bounds(
                deadline_s=300.0, clock=clock
            ),
        )
        # A backward jump EXTENDS the envelope; the run finishes honestly.
        assert run.status == WorkflowRunStatus.COMPLETED
        envelope = executor.last_envelope
        assert envelope is not None
        assert envelope.nodes_executed == 3
        assert fake.executes == {"start": 1, "mid": 1, "end": 1}
        assert elapsed < FAULT_RESOLUTION_LIMIT_S
        assert_run_finished(run, elapsed_s=elapsed)

    async def test_deadline_already_spent_cancels_with_zero_nodes(self) -> None:
        plan, revision = _linear()
        start = time.monotonic()
        # Call 1 anchors the envelope; call 2 (the first deadline check)
        # jumps 1000 s against a 1 s deadline.
        clock = SkewClock(jump_after_calls=2, jump_s=1000.0, start=start)
        run, fake, _, elapsed = await _drive(
            plan, revision, {}, run_id="f-spent",
            configure=lambda ex: ex.configure_run_bounds(
                deadline_s=1.0, clock=clock
            ),
        )
        assert run.status == WorkflowRunStatus.CANCELLED
        assert "run deadline reached" in (run.error or "")
        assert fake.executes == {}  # no node executed at all
        assert run.node_runs["start"].status == NodeRunStatus.SKIPPED
        assert elapsed < FAULT_RESOLUTION_LIMIT_S

    async def test_cancel_during_node_execution_settles_in_flight(self) -> None:
        """External task cancellation mid-node settles the in-flight node and
        propagates (asyncio semantics) — no COMPLETED, no hang."""

        class _Hanging:
            def __init__(self) -> None:
                self.calls = 0

            async def execute_node(self, node, input_data, workspace_id):
                self.calls += 1
                await asyncio.sleep(30)
                return {"output": "never"}

        plan, revision = _linear()
        fake = _Hanging()
        executor = WorkflowExecutor(fake)
        task = asyncio.create_task(
            executor.execute(plan, revision, {"input": "x"}, run_id="f-ext")
        )
        await asyncio.sleep(0)
        task.cancel()
        started = time.perf_counter()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.perf_counter() - started
        assert elapsed < FAULT_RESOLUTION_LIMIT_S
        assert fake.calls == 1


# ---------------------------------------------------------------------------
# Hooks off by default — the production path is untouched
# ---------------------------------------------------------------------------


class TestFaultHooksOffByDefault:
    async def test_default_executor_runs_with_stated_defaults(self) -> None:
        """No fault hooks armed by default: the run carries the stated
        envelope constants, the AGENT seam is unwired and the checkpoint
        failure counter is 0 (nothing injected)."""
        executor = WorkflowExecutor(ScriptedNodeExecutor({}))
        assert executor.agent_executor is None
        assert executor.checkpoint_failures == 0
        plan, revision = _linear()
        run = await executor.execute(plan, revision, {"input": "x"}, run_id="f-def")
        envelope = executor.last_envelope
        assert envelope is not None
        assert envelope.deadline_s == RUN_DEADLINE_S
        assert envelope.max_node_seconds == RUN_MAX_NODE_SECONDS
        assert envelope.max_nodes == RUN_MAX_NODES
        assert NODE_TIMEOUT_S == 30.0  # the inner-layer default, unchanged
        assert run.status == WorkflowRunStatus.COMPLETED

    async def test_default_path_run_is_unchanged(self) -> None:
        """Without any fault injection the run is the plain, honest one."""
        plan, revision = _linear()
        run, fake, _, elapsed = await _drive(plan, revision, {}, run_id="f-default")
        assert run.status == WorkflowRunStatus.COMPLETED
        assert fake.executes == {"start": 1, "mid": 1, "end": 1}
        assert run.final_output == "end-out"
        assert elapsed < FAULT_RESOLUTION_LIMIT_S

    def test_fault_module_imports_no_hooks_into_the_executor(self) -> None:
        """The matrix module is test-only infrastructure: importing it must not
        change the executor's default configuration surface."""
        import workflow_engine.executor as executor_module
        import workflow_engine.fault_matrix  # noqa: F401

        for name in ("ScriptedNodeExecutor", "SkewClock", "FailingCheckpointStore"):
            assert not hasattr(executor_module, name), (
                f"fault hook {name} leaked into the executor module"
            )
