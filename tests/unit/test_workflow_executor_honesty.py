"""T-V4-009 — executor honesty: terminal states, no false-COMPLETED.

The hostile battery proves no shape ends COMPLETED when it should not:

* a node raises (NotImplementedError and a generic Exception),
* a node returns an ``{"error": ...}`` payload,
* cancellation mid-run (``request_cancel`` from inside a node),
* a declared join barrier left incomplete (all-barrier with a skipped
  branch; any/first-barrier with no active input).

And the no-over-rejection half: genuine runs still COMPLETE, routing skips
(conditional branch not taken on a single-inbound node) stay disclosed and
may complete, and a 2/2 active barrier merge completes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from workflow_engine import (
    JoinStrategy,
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


def _node(
    node_id: str,
    node_type: NodeType,
    *,
    in_ports: tuple[str, ...] = ("input",),
    out_ports: tuple[str, ...] = ("output",),
    join: JoinStrategy | None = None,
    config: dict | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        type=node_type,
        config=config or {},
        input_ports=[PortSpec(name=p, type="any") for p in in_ports],
        output_ports=[PortSpec(name=p, type="any") for p in out_ports],
        join_strategy=join,
    )


def _edge(edge_id: str, source: str, source_port: str, target: str, target_port: str, *, condition: Any = None) -> WorkflowEdgeSpec:  # noqa: E501
    return WorkflowEdgeSpec(
        id=edge_id,
        source_node_id=source,
        source_port=source_port,
        target_node_id=target,
        target_port=target_port,
        condition_value=condition,
    )


class _ScriptedExecutor:
    """NodeExecutor whose per-node behaviour is scripted."""

    def __init__(self, behaviours: dict[str, Any]) -> None:
        self._behaviours = behaviours
        self.calls: list[str] = []

    async def execute_node(
        self,
        node: NodeSpec,
        input_data: dict[str, Any] | Any,
        workspace_id: str,
    ) -> dict[str, Any]:
        self.calls.append(node.id)
        behaviour = self._behaviours.get(node.id, "ok")
        if behaviour == "raise":
            raise RuntimeError(f"{node.id} exploded")
        if behaviour == "not-implemented":
            raise NotImplementedError(f"{node.id} unavailable")
        if behaviour == "error-payload":
            return {"error": f"{node.id} failed semantically"}
        if node.type == NodeType.TRIGGER:
            return {"output": input_data}
        if node.type == NodeType.CONDITION:
            field = node.config.get("field")
            expected = node.config.get("expected")
            value = (
                input_data.get(field)
                if isinstance(input_data, dict) and field is not None
                else input_data
            )
            return {"result": value == expected, "output": input_data}
        return {"output": f"{node.id}-out"}


def _compile(nodes: list[NodeSpec], edges: list[WorkflowEdgeSpec]) -> tuple:
    spec = WorkflowDefinition(
        id="wf-honesty", workspace_id="ws-1", name="Honesty",
        nodes=nodes, edges=edges,
    )
    revision = WorkflowRevision.create(spec, revision=1)
    plan = WorkflowCompiler.compile(revision)
    return plan, revision


def _linear(**node_behaviours: Any) -> tuple:
    """trigger → mid → output, with per-node behaviours."""
    nodes = [
        _node("start", NodeType.TRIGGER, in_ports=()),
        _node("mid", NodeType.TRANSFORM),
        _node("end", NodeType.OUTPUT),
    ]
    edges = [
        _edge("e1", "start", "output", "mid", "input"),
        _edge("e2", "mid", "output", "end", "input"),
    ]
    plan, revision = _compile(nodes, edges)
    return plan, revision, node_behaviours


async def _run(plan, revision, behaviours: dict[str, Any], initial: dict | None = None) -> tuple:
    executor = WorkflowExecutor(_ScriptedExecutor(behaviours))
    run = await executor.execute(plan, revision, initial or {"input": "x"})
    return run, executor


# ---------------------------------------------------------------------------
# Hostile battery — none of these end COMPLETED
# ---------------------------------------------------------------------------


class TestHostileBatteryNeverCompleted:
    async def test_node_raises_runtime_error(self) -> None:
        plan, revision, behaviours = _linear(mid="raise")
        run, _ = await _run(plan, revision, behaviours)
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id == "mid"
        assert "mid exploded" in (run.error or "")
        assert run.node_runs["mid"].status == NodeRunStatus.FAILED
        assert run.node_runs["end"].status == NodeRunStatus.SKIPPED

    async def test_node_raises_not_implemented(self) -> None:
        plan, revision, behaviours = _linear(mid="not-implemented")
        run, _ = await _run(plan, revision, behaviours)
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id == "mid"
        assert "unavailable" in (run.error or "")

    async def test_node_returns_error_payload(self) -> None:
        """A returned error is a failure — never absorbed into outputs."""
        plan, revision, behaviours = _linear(mid="error-payload")
        run, _ = await _run(plan, revision, behaviours)
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id == "mid"
        assert "failed semantically" in (run.error or "")
        assert run.final_output is None
        assert run.node_runs["mid"].status == NodeRunStatus.FAILED

    async def test_cancel_mid_run_ends_cancelled_not_completed(self) -> None:
        """Cancellation requested from inside a node: the run ends CANCELLED,
        the completed node keeps its recorded output, and nothing starts
        afterwards — never COMPLETED-after-cancel."""
        plan, revision, _ = _linear()
        cancel_holder: dict[str, WorkflowExecutor] = {}

        class _CancellingExecutor:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def execute_node(self, node, input_data, workspace_id):
                self.calls.append(node.id)
                if node.id == "start":
                    # Request cancellation while the run is in flight.
                    cancel_holder["executor"].request_cancel()
                return {"output": f"{node.id}-out"}

        fake = _CancellingExecutor()
        executor = WorkflowExecutor(fake)
        cancel_holder["executor"] = executor
        run = await executor.execute(plan, revision, {"input": "x"})

        assert fake.calls == ["start"], "no node starts after a cancel request"
        assert run.status == WorkflowRunStatus.CANCELLED
        assert run.error == "run cancelled by request"
        # In-flight node settled: its output is recorded, not lost.
        assert run.node_runs["start"].status == NodeRunStatus.COMPLETED
        assert run.node_runs["start"].output_data == {"output": "start-out"}
        # Remaining nodes skipped with the cancellation reason disclosed.
        assert run.node_runs["mid"].status == NodeRunStatus.SKIPPED
        assert "cancelled" in (run.node_runs["mid"].error or "")
        assert run.node_runs["end"].status == NodeRunStatus.SKIPPED
        assert run.finished_at

    async def test_all_barrier_partial_inputs_fail_run(self) -> None:
        """A declared all-barrier left incomplete is not a silent success."""
        nodes = [
            _node("start", NodeType.TRIGGER, in_ports=()),
            _node("cond", NodeType.CONDITION, out_ports=("result", "output"), config={"field": "mode", "expected": "fast"}),  # noqa: E501
            _node("fast", NodeType.TRANSFORM),
            _node("slow", NodeType.TRANSFORM),
            _node("merge", NodeType.TRANSFORM, in_ports=("a", "b"), join=JoinStrategy.ALL),
            _node("end", NodeType.OUTPUT),
        ]
        edges = [
            _edge("e1", "start", "output", "cond", "input"),
            _edge("e2", "cond", "result", "fast", "input", condition=True),
            _edge("e3", "cond", "result", "slow", "input", condition=False),
            _edge("e4", "fast", "output", "merge", "a"),
            _edge("e5", "slow", "output", "merge", "b"),
            _edge("e6", "merge", "output", "end", "input"),
        ]
        plan, revision = _compile(nodes, edges)
        run, _ = await _run(plan, revision, {}, {"mode": "fast"})
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id == "merge"
        assert "barrier" in (run.error or "")
        assert run.node_runs["merge"].status == NodeRunStatus.SKIPPED
        assert "barrier-incomplete" in (run.node_runs["merge"].error or "")

    async def test_any_barrier_no_active_input_fails_run(self) -> None:
        nodes = [
            _node("start", NodeType.TRIGGER, in_ports=()),
            _node("cond", NodeType.CONDITION, out_ports=("result", "output"), config={"field": "mode", "expected": "fast"}),  # noqa: E501
            _node("a", NodeType.TRANSFORM),
            _node("b", NodeType.TRANSFORM),
            _node("join", NodeType.TRANSFORM, in_ports=("a", "b"), join=JoinStrategy.ANY),
            _node("end", NodeType.OUTPUT),
        ]
        edges = [
            _edge("e1", "start", "output", "cond", "input"),
            _edge("e2", "cond", "result", "a", "input", condition=True),
            _edge("e3", "cond", "result", "b", "input", condition=False),
            _edge("e4", "a", "output", "join", "a"),
            _edge("e5", "b", "output", "join", "b"),
            _edge("e6", "join", "output", "end", "input"),
        ]
        # The condition computes False and NEITHER conditional edge matches:
        # no branch activates, so the any-barrier join receives no input.
        nodes[1].config = {"field": "mode", "expected": "other"}
        edges[2] = _edge("e3", "cond", "result", "b", "input", condition="neither")
        plan, revision = _compile(nodes, edges)
        run, _ = await _run(plan, revision, {}, {"mode": "fast"})
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id == "join"
        assert "barrier-incomplete" in (run.node_runs["join"].error or "")


# ---------------------------------------------------------------------------
# No over-rejection — genuine runs still COMPLETE
# ---------------------------------------------------------------------------


class TestGenuineCompletions:
    async def test_clean_linear_run_completes(self) -> None:
        plan, revision, behaviours = _linear()
        run, _ = await _run(plan, revision, behaviours)
        assert run.status == WorkflowRunStatus.COMPLETED
        assert run.error is None
        assert run.failed_node_id is None
        assert run.final_output == "end-out"
        assert all(
            nr.status == NodeRunStatus.COMPLETED for nr in run.node_runs.values()
        )

    async def test_routing_skip_single_inbound_still_completes(self) -> None:
        """A conditional branch not taken on a single-inbound node is routing,
        disclosed SKIPPED, and the run may still complete honestly."""
        nodes = [
            _node("start", NodeType.TRIGGER, in_ports=()),
            _node("cond", NodeType.CONDITION, out_ports=("result", "output"), config={"field": "doc_type", "expected": "legal"}),  # noqa: E501
            _node("legal_out", NodeType.OUTPUT),
            _node("general_out", NodeType.OUTPUT),
        ]
        edges = [
            _edge("e1", "start", "output", "cond", "input"),
            _edge("e2", "cond", "result", "legal_out", "input", condition=True),
            _edge("e3", "cond", "result", "general_out", "input", condition=False),
        ]
        plan, revision = _compile(nodes, edges)
        run, _ = await _run(plan, revision, {}, {"doc_type": "legal"})
        assert run.status == WorkflowRunStatus.COMPLETED
        assert run.node_runs["legal_out"].status == NodeRunStatus.COMPLETED
        assert run.node_runs["general_out"].status == NodeRunStatus.SKIPPED
        assert "no-active-inbound-edge" in (run.node_runs["general_out"].error or "")

    async def test_any_barrier_with_active_input_completes(self) -> None:
        nodes = [
            _node("start", NodeType.TRIGGER, in_ports=()),
            _node("cond", NodeType.CONDITION, out_ports=("result", "output"), config={"field": "mode", "expected": "fast"}),  # noqa: E501
            _node("a", NodeType.TRANSFORM),
            _node("b", NodeType.TRANSFORM),
            _node("join", NodeType.TRANSFORM, in_ports=("a", "b"), join=JoinStrategy.ANY),
            _node("end", NodeType.OUTPUT),
        ]
        edges = [
            _edge("e1", "start", "output", "cond", "input"),
            _edge("e2", "cond", "result", "a", "input", condition=True),
            _edge("e3", "cond", "result", "b", "input", condition=False),
            _edge("e4", "a", "output", "join", "a"),
            _edge("e5", "b", "output", "join", "b"),
            _edge("e6", "join", "output", "end", "input"),
        ]
        plan, revision = _compile(nodes, edges)
        run, _ = await _run(plan, revision, {}, {"mode": "fast"})
        # any-barrier: first arrival satisfies the declared barrier.
        assert run.status == WorkflowRunStatus.COMPLETED
        assert run.node_runs["join"].status == NodeRunStatus.COMPLETED

    async def test_full_all_barrier_completes(self) -> None:
        nodes = [
            _node("start", NodeType.TRIGGER, in_ports=()),
            _node("a", NodeType.TRANSFORM),
            _node("b", NodeType.TRANSFORM),
            _node("merge", NodeType.TRANSFORM, in_ports=("a", "b"), join=JoinStrategy.ALL),
            _node("end", NodeType.OUTPUT),
        ]
        edges = [
            _edge("e1", "start", "output", "a", "input"),
            _edge("e2", "start", "output", "b", "input"),
            _edge("e3", "a", "output", "merge", "a"),
            _edge("e4", "b", "output", "merge", "b"),
            _edge("e5", "merge", "output", "end", "input"),
        ]
        plan, revision = _compile(nodes, edges)
        run, _ = await _run(plan, revision, {})
        assert run.status == WorkflowRunStatus.COMPLETED
        assert run.node_runs["merge"].status == NodeRunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Terminal-state machine — the table is closed
# ---------------------------------------------------------------------------


class TestTerminalStateMachine:
    async def test_every_terminal_status_reachable_and_exclusive(self) -> None:
        statuses = {
            WorkflowRunStatus.COMPLETED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        }
        assert statuses == set(WorkflowRunStatus) - {
            WorkflowRunStatus.PENDING, WorkflowRunStatus.RUNNING
        }

    async def test_run_reaches_exactly_one_terminal_status(self) -> None:
        """Each battery run ends in exactly one terminal status with a
        finished_at timestamp and no RUNNING residue."""
        plan, revision, behaviours = _linear(mid="raise")
        failed_run, _ = await _run(plan, revision, behaviours)
        plan2, revision2, behaviours2 = _linear()
        clean_run, _ = await _run(plan2, revision2, behaviours2)
        runs = [failed_run, clean_run]
        for run in runs:
            terminal = {
                WorkflowRunStatus.COMPLETED,
                WorkflowRunStatus.FAILED,
                WorkflowRunStatus.CANCELLED,
            }
            assert run.status in terminal
            assert run.finished_at
            for record in run.node_runs.values():
                assert record.status != NodeRunStatus.RUNNING, (
                    f"node {record.node_id} left RUNNING in a finished run"
                )
                assert record.status != NodeRunStatus.PENDING

    async def test_external_task_cancellation_settles_and_propagates(self) -> None:
        """An external asyncio cancellation settles the in-flight node and
        re-raises — the executor never swallows it, never reports COMPLETED."""
        plan, revision, _ = _linear()

        class _HangingExecutor:
            async def execute_node(self, node, input_data, workspace_id):
                if node.id == "mid":
                    await asyncio.sleep(30)
                return {"output": "ok"}

        executor = WorkflowExecutor(_HangingExecutor())
        task = asyncio.create_task(executor.execute(plan, revision, {"input": "x"}))
        await asyncio.sleep(0)  # let it reach the hanging node
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
