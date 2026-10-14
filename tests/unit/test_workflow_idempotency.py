"""T-V4-013 — executor idempotency + cancel/resume + duplicate-delivery.

IDEMPOTENCY RULE (stated): the key is ``(run_id, node_id)``; the fingerprint
is the canonical form of the work the key stands for (node id, type, config,
input).  The ledger is consulted BEFORE any execution:

* key unseen                      → execute, then record;
* key seen, SAME fingerprint      → replay the recorded output (effect once,
                                    recorded once — ``executes == 0``);
* key seen, DIFFERENT fingerprint → :class:`IdempotencyKeyConflict`, loudly.

Cancel/resume: the settled set is checkpointed after every settled node; a
resume replays settled nodes from the checkpoint (never re-executed), seeds
the ledger from the checkpoint, and continues the unsettled remainder
exactly.  Duplicate deliveries dedupe through the same ledger, because a
redelivery and a resume are the same event: "execute this key again".

All three metrics: duplicate effects: 0; resume re-runs settled nodes: 0;
unsettled nodes skipped on resume: 0.
"""

from __future__ import annotations

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
from workflow_engine.checkpoint import InMemoryCheckpointStore
from workflow_engine.executor import IdempotencyKeyConflict, IdempotencyLedger

#: Wall-clock ceiling for the "settled fast" proofs.
REFUSAL_TIME_LIMIT_S = 5.0


class _CountingExecutor:
    """Primary executor that counts real executions per node."""

    def __init__(self) -> None:
        self.executes: dict[str, int] = {}
        self.cancelled_at: str | None = None

    async def execute_node(
        self,
        node: NodeSpec,
        input_data: dict[str, Any] | Any,
        workspace_id: str,
    ) -> dict[str, Any]:
        self.executes[node.id] = self.executes.get(node.id, 0) + 1
        if node.type is NodeType.TRIGGER:
            return {"output": input_data}
        return {"output": f"{node.id}-out"}


def _node(
    node_id: str,
    node_type: NodeType,
    *,
    in_ports: tuple[str, ...] = ("input",),
    out_ports: tuple[str, ...] = ("output",),
    config: dict | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        type=node_type,
        config=config or {},
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
        id="wf-idem", workspace_id="ws-1", name="Idem",
        nodes=nodes, edges=edges,
    )
    revision = WorkflowRevision.create(spec, revision=1)
    return WorkflowCompiler.compile(revision), revision


# ---------------------------------------------------------------------------
# Duplicate delivery — deduplicated by idempotency key
# ---------------------------------------------------------------------------


class TestDuplicateDelivery:
    async def test_double_dispatch_same_key_executes_once(self) -> None:
        """Duplicate effects: 0 — the same (run_id, node_id) delivered twice
        executes once and records once; the second delivery replays."""
        plan, revision = _linear()
        fake = _CountingExecutor()
        executor = WorkflowExecutor(fake)
        first = await executor.execute(plan, revision, {"input": "x"}, run_id="run-A")
        assert first.status == WorkflowRunStatus.COMPLETED
        counts_after_first = dict(fake.executes)

        # Duplicate delivery of the same run: every node is already settled
        # under its key → replayed, never re-executed.
        second = await executor.execute(plan, revision, {"input": "x"}, run_id="run-A")
        assert second.status == WorkflowRunStatus.COMPLETED
        assert fake.executes == counts_after_first
        assert fake.executes == {"start": 1, "mid": 1, "end": 1}
        # The replayed run records the same outputs.
        assert second.final_output == first.final_output

    async def test_different_run_ids_do_not_collide(self) -> None:
        plan, revision = _linear()
        fake = _CountingExecutor()
        executor = WorkflowExecutor(fake)
        await executor.execute(plan, revision, {"input": "x"}, run_id="run-A")
        await executor.execute(plan, revision, {"input": "x"}, run_id="run-B")
        # Different runs are different work: each node executed once per run.
        assert fake.executes == {"start": 2, "mid": 2, "end": 2}

    async def test_same_key_different_work_conflicts_loudly(self) -> None:
        """A key presented for different work raises — never a silent hit."""
        ledger = IdempotencyLedger()
        fake = _CountingExecutor()
        executor = WorkflowExecutor(fake)
        executor.configure_idempotency_ledger(ledger)
        plan, revision = _linear()
        await executor.execute(plan, revision, {"input": "x"}, run_id="run-A")
        # Same run id, different input for the same node → fingerprint mismatch.
        with pytest.raises(IdempotencyKeyConflict):
            await executor.execute(plan, revision, {"input": "DIFFERENT"}, run_id="run-A")

    async def test_ledger_records_one_entry_per_key(self) -> None:
        ledger = IdempotencyLedger()
        fake = _CountingExecutor()
        executor = WorkflowExecutor(fake)
        executor.configure_idempotency_ledger(ledger)
        plan, revision = _linear()
        await executor.execute(plan, revision, {"input": "x"}, run_id="run-A")
        await executor.execute(plan, revision, {"input": "x"}, run_id="run-A")
        assert ledger.keys() == {("run-A", "start"), ("run-A", "mid"), ("run-A", "end")}


# ---------------------------------------------------------------------------
# Cancel + resume — settled nodes never re-run, unsettled never skipped
# ---------------------------------------------------------------------------


class _CancelAtExecutor:
    """Primary executor that cancels the RUNNING executor mid-run.

    ``sink`` is a one-element list filled with the executor being driven
    (construction order forces the holder — the cancelling executor exists
    before the executor that runs it).
    """

    def __init__(self, cancel_after: str, sink: list) -> None:
        self.cancel_after = cancel_after
        self.sink = sink
        self.executes: dict[str, int] = {}

    async def execute_node(
        self,
        node: NodeSpec,
        input_data: dict[str, Any] | Any,
        workspace_id: str,
    ) -> dict[str, Any]:
        self.executes[node.id] = self.executes.get(node.id, 0) + 1
        if node.type is NodeType.TRIGGER:
            return {"output": input_data}
        if node.id == self.cancel_after:
            self.sink[0].request_cancel()
        return {"output": f"{node.id}-out"}


def _cancelling_executor(cancel_after: str, executes: dict[str, int]) -> tuple:
    """Build (executor, cancelling fake) wired so the cancel lands on the loop."""
    holder: list[WorkflowExecutor] = []
    fake = _CancelAtExecutor(cancel_after, holder)
    fake.executes = executes
    executor = WorkflowExecutor(fake)
    holder.append(executor)
    return executor, fake


class TestCancelResume:
    async def _baseline(self) -> tuple:
        plan, revision = _linear()
        fake = _CountingExecutor()
        executor = WorkflowExecutor(fake)
        run = await executor.execute(plan, revision, {"input": "x"}, run_id="base")
        return run, fake, plan, revision

    async def test_cancel_mid_run_settles_and_checkpoints(self) -> None:
        plan, revision = _linear()
        store = InMemoryCheckpointStore()
        # Cancel after `mid` completes: the run ends CANCELLED, `end` skipped.
        executor, fake = _cancelling_executor("mid", {})
        executor.configure_checkpoint_store(store)
        run = await executor.execute(plan, revision, {"input": "x"}, run_id="run-C")
        assert fake.executes == {"start": 1, "mid": 1}  # end never started
        assert run.status == WorkflowRunStatus.CANCELLED
        assert run.node_runs["mid"].status == NodeRunStatus.COMPLETED
        assert run.node_runs["end"].status == NodeRunStatus.SKIPPED
        # The settled set is checkpointed at the cancel boundary.
        checkpoint = await store.latest("run-C")
        assert checkpoint is not None
        assert set(checkpoint.completed_nodes) == {"start", "mid"}

    async def test_resume_replays_settled_and_continues_exactly(self) -> None:
        """Resume re-runs settled nodes: 0 and unsettled skipped: 0 — the
        resumed run's settled set, continuation and final output match a
        never-interrupted baseline exactly."""
        plan, revision = _linear()
        store = InMemoryCheckpointStore()
        executor, fake = _cancelling_executor("mid", {})
        executor.configure_checkpoint_store(store)
        cancelled = await executor.execute(plan, revision, {"input": "x"}, run_id="run-R")
        assert fake.executes == {"start": 1, "mid": 1}
        assert cancelled.status == WorkflowRunStatus.CANCELLED
        settled_before = set(cancelled.node_runs.keys()) - {"end"}

        # Fresh executor (a "new worker"); the same durable store.
        resume_fake = _CountingExecutor()
        resume_executor = WorkflowExecutor(resume_fake)
        resume_executor.configure_checkpoint_store(store)
        resumed = await resume_executor.execute(
            plan, revision, {"input": "x"}, run_id="run-R", resume=True
        )

        # Settled nodes were NOT re-executed on the resume worker.
        assert resume_fake.executes.get("start") is None
        assert resume_fake.executes.get("mid") is None
        assert resume_fake.executes.get("end") == 1  # unsettled: ran, not skipped
        # The settled set is identical (replayed from the checkpoint).
        assert set(resumed.node_runs) == {"start", "mid", "end"}
        assert set(settled_before) <= set(resumed.node_runs)
        assert resumed.node_runs["start"].status == NodeRunStatus.COMPLETED
        assert resumed.node_runs["mid"].status == NodeRunStatus.COMPLETED
        assert resumed.node_runs["mid"].output_data == {"output": "mid-out"}
        # Continuation exact: the resumed run completes with the same output.
        assert resumed.status == WorkflowRunStatus.COMPLETED
        assert resumed.final_output == "end-out"
        # The ledger was re-seeded from the checkpoint, so a duplicate
        # delivery after the resume still replays.
        again = await resume_executor.execute(
            plan, revision, {"input": "x"}, run_id="run-R", resume=True
        )
        assert resume_fake.executes.get("end") == 1  # still once
        assert again.final_output == "end-out"

    async def test_resume_matches_never_interrupted_baseline(self) -> None:
        plan, revision = _linear()
        baseline_fake = _CountingExecutor()
        baseline = await WorkflowExecutor(baseline_fake).execute(
            plan, revision, {"input": "x"}, run_id="base"
        )
        store = InMemoryCheckpointStore()
        cancelling_fake: dict[str, int] = {}
        executor2, fake = _cancelling_executor("mid", cancelling_fake)
        executor2.configure_checkpoint_store(store)
        await executor2.execute(plan, revision, {"input": "x"}, run_id="run-M")
        resume_fake = _CountingExecutor()
        resume_executor = WorkflowExecutor(resume_fake)
        resume_executor.configure_checkpoint_store(store)
        resumed = await resume_executor.execute(
            plan, revision, {"input": "x"}, run_id="run-M", resume=True
        )
        # Node-for-node identical outcomes, including the outputs.
        assert {
            nid: (rec.status, rec.output_data)
            for nid, rec in resumed.node_runs.items()
        } == {
            nid: (rec.status, rec.output_data)
            for nid, rec in baseline.node_runs.items()
        }
        assert resumed.final_output == baseline.final_output
        # Every node executed exactly once across the two workers combined.
        assert fake.executes == {"start": 1, "mid": 1}
        assert resume_fake.executes == {"end": 1}

    async def test_resume_rejects_foreign_workspace_and_revision(self) -> None:
        from workflow_engine.checkpoint import ResumeStateError, WorkflowCheckpoint

        plan, revision = _linear()
        store = InMemoryCheckpointStore()
        await store.append(
            WorkflowCheckpoint(
                run_id="run-X",
                workflow_id=revision.spec.id,
                revision_id=plan.revision_hash,
                workspace_id="ws-OTHER",
                completed_nodes=["start"],
                node_outputs={"start": {"output": {"input": "x"}}},
                revision_hash=plan.revision_hash,
            )
        )
        executor = WorkflowExecutor(_CountingExecutor())
        executor.configure_checkpoint_store(store)
        with pytest.raises(ResumeStateError):
            await executor.execute(
                plan, revision, {"input": "x"}, run_id="run-X", resume=True
            )

    async def test_checkpoint_write_failure_is_counted_not_fatal(self) -> None:
        """A failing checkpoint store never kills the run — and never stays
        silent (``checkpoint_failures``)."""

        class _BrokenStore(InMemoryCheckpointStore):
            async def append(self, checkpoint):
                raise RuntimeError("store down")

        plan, revision = _linear()
        executor = WorkflowExecutor(_CountingExecutor())
        executor.configure_checkpoint_store(_BrokenStore())
        run = await executor.execute(plan, revision, {"input": "x"}, run_id="run-F")
        assert run.status == WorkflowRunStatus.COMPLETED
        assert executor.checkpoint_failures >= 1

    async def test_no_store_means_no_checkpoints_and_no_resume(self) -> None:
        plan, revision = _linear()
        executor = WorkflowExecutor(_CountingExecutor())
        # No store configured: resume finds nothing and runs normally.
        run = await executor.execute(
            plan, revision, {"input": "x"}, run_id="run-N", resume=True
        )
        assert run.status == WorkflowRunStatus.COMPLETED
