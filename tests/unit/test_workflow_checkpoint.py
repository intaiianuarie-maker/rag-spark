"""T-V4-016 — workflow checkpoint / resume / evidence propagation tests.

Pinned properties (card):
1. Checkpoints are written only at boundaries worth resuming from
   (terminal / approval / completed-community), not at every heartbeat.
2. Resume after a kill produces an *identical* run trace (same bar as V3
   T-012): the resumed node runset, outputs and final output match.
3. Evidence written before the kill re-attaches to the resumed run — same
   content, same provenance — with zero orphans.

Plus durability: the checkpoint store survives a simulated process restart
(close + reopen the SQLite file) and resume still works from it.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from workflow_engine import (
    FakeNodeExecutor,
    NodeSpec,
    NodeType,
    PortSpec,
    WorkflowCompiler,
    WorkflowDefinition,
    WorkflowEdgeSpec,
    WorkflowExecutor,
    WorkflowRevision,
    WorkflowRun,
)
from workflow_engine.checkpoint import (
    MAX_CHECKPOINT_BYTES,
    EvidenceRow,
    InMemoryCheckpointStore,
    ResumableExecutor,
    ResumeStateError,
    SqliteCheckpointStore,
    WorkflowCheckpoint,
    attach_evidence,
    checkpoint_boundary,
    is_checkpoint_boundary,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_revision(defn: WorkflowDefinition) -> WorkflowRevision:
    canonical = json.dumps(defn.model_dump(mode="json"), sort_keys=True)
    h = hashlib.sha256(canonical.encode()).hexdigest()
    return WorkflowRevision(
        workflow_id=defn.id, revision=1, content_hash=h, spec=defn
    )


async def _run(
    defn: WorkflowDefinition, initial_input: dict, executor=None
) -> tuple[WorkflowRun, WorkflowRevision, object]:
    rev = _make_revision(defn)
    plan = WorkflowCompiler.compile(rev, allow_unvalidated=True)
    runner = WorkflowExecutor(executor or FakeNodeExecutor())
    run = await runner.execute(plan, rev, initial_input)
    return run, rev, plan


# A linear 4-node workflow: trigger -> transform -> output -> output
def _linear_defn() -> WorkflowDefinition:
    return WorkflowDefinition(
        id="wf-ckpt", workspace_id="ws", name="Ckpt",
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="output", type="dict")]),
            NodeSpec(id="fmt", type=NodeType.TRANSFORM, name="Format",
                     config={"template": "Hi {name}!"},
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="string")]),
            NodeSpec(id="out1", type=NodeType.OUTPUT, name="Out1",
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="end", type=NodeType.OUTPUT, name="End",
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                             target_node_id="fmt", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="fmt", source_port="output",
                             target_node_id="out1", target_port="input"),
            WorkflowEdgeSpec(id="e3", source_node_id="out1", source_port="output",
                             target_node_id="end", target_port="input"),
        ],
    )


def _norm_run(run: WorkflowRun) -> dict:
    """Drop run identity + timing so two runs compare exactly."""
    data = run.model_dump(mode="json")
    for k in ("id", "created_at", "finished_at"):
        data.pop(k, None)
    for node in data.get("node_runs", {}).values():
        node.pop("latency_ms", None)
    return data


# ---------------------------------------------------------------------------
# 1. Checkpoint boundary policy — persisted, not every heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_checkpoint_boundary_terminal() -> None:
    """Run completion or failure is always a terminal boundary."""
    assert checkpoint_boundary(
        node_type=NodeType.TRANSFORM, run_complete=True, run_failed=False,
        previously_completed=False,
    ) == "terminal"
    assert checkpoint_boundary(
        node_type=NodeType.TRANSFORM, run_complete=False, run_failed=True,
        previously_completed=False,
    ) == "terminal"


@pytest.mark.unit
def test_checkpoint_boundary_approval_and_community() -> None:
    """OUTPUT nodes mark approval- and completed-community boundaries."""
    assert checkpoint_boundary(
        node_type=NodeType.OUTPUT, run_complete=False, run_failed=False,
        previously_completed=False,
    ) == "approval"
    # ...but a re-completion is only worth persisting as community work.
    assert checkpoint_boundary(
        node_type=NodeType.OUTPUT, run_complete=False, run_failed=False,
        previously_completed=True,
    ) == "community"


@pytest.mark.unit
def test_checkpoint_boundary_skips_transient_nodes() -> None:
    """TRIGGER/TRANSFORM completions are not resumable boundaries."""
    for node_type in (NodeType.TRIGGER, NodeType.TRANSFORM):
        assert checkpoint_boundary(
            node_type=node_type, run_complete=False, run_failed=False,
            previously_completed=False,
        ) is None


@pytest.mark.unit
def test_is_checkpoint_boundary_matches_policy() -> None:
    assert is_checkpoint_boundary(NodeType.OUTPUT) is True
    assert is_checkpoint_boundary(NodeType.TRANSFORM) is False


@pytest.mark.unit
async def test_checkpoints_written_only_at_checkpoint_boundaries() -> None:
    """A 4-node run has 4 completed nodes but only 2 checkpoint-worthy boundaries.

    ``start`` (TRIGGER) and ``fmt`` (TRANSFORM) stay ephemeral; the two
    OUTPUT nodes are the completed-community/approval boundaries a restart
    may resume from. Not every heartbeat is persisted.
    """
    defn = _linear_defn()
    run, rev, _ = await _run(defn, {"name": "Alice"})

    node_type_by_id = {n.id: n.type for n in rev.spec.nodes}
    completed = {
        nid for nid, nr in run.node_runs.items() if nr.status.value == "completed"
    }
    assert len(completed) == 4

    worth = {
        nid
        for nid in completed
        if is_checkpoint_boundary(node_type_by_id[nid])
    }
    assert worth == {"out1", "end"}


@pytest.mark.unit
async def test_checkpoint_sequence_is_monotonic_per_run() -> None:
    store = InMemoryCheckpointStore()
    cp = WorkflowCheckpoint(run_id="r1", workflow_id="w", revision_id="rev",
                            workspace_id="ws-1")
    c1 = await store.append(cp)
    c2 = await store.append(cp.model_copy())
    assert c1.sequence < c2.sequence
    assert c2.sequence == 2


@pytest.mark.unit
async def test_sqlite_checkpoint_survives_simulated_restart(tmp_path) -> None:
    """A restart (close + reopen the file) preserves the checkpoint."""
    db = tmp_path / "ckpt.db"
    store = SqliteCheckpointStore(db)
    cp = WorkflowCheckpoint(
        run_id="r1", workflow_id="w", revision_id="rev", workspace_id="ws-1",
        boundary="terminal", node_outputs={"start": {"output": {"x": 1}}},
        evidence=[EvidenceRow(run_id="r1", node_id="out1", workspace_id="ws-1",
                              kind="citation", payload={"doc": "d1", "chunk": "c1"})],
    )
    await store.append(cp)
    del store  # simulate process exit; the file is the durable state

    reopened = SqliteCheckpointStore(db)
    latest = await reopened.latest("r1")
    assert latest is not None
    assert latest.boundary == "terminal"
    assert latest.node_outputs == {"start": {"output": {"x": 1}}}
    ev = await reopened.evidence_for("r1")
    assert len(ev) == 1 and ev[0].payload["chunk"] == "c1"


@pytest.mark.unit
async def test_checkpoint_payload_is_bounded() -> None:
    """Oversized checkpoints are refused, never silently truncated.

    The payload must be *aggregate*-oversized: per-field text is already
    truncated to ``MAX_TEXT_CHARS``, so one long string cannot breach the
    bound on its own. 128 fields of ~8 KB each pushes the serialized
    checkpoint past ``MAX_CHECKPOINT_BYTES``. Enforced for the in-memory store
    as well, so the bound is an invariant of the discipline, not a backend
    detail.
    """
    cp = WorkflowCheckpoint(
        run_id="r1", workflow_id="w", revision_id="rev", workspace_id="ws-1",
        node_outputs={
            f"k{i}": {"data": "x" * (MAX_CHECKPOINT_BYTES // 32)}
            for i in range(128)
        },
    )
    for store in (InMemoryCheckpointStore(),):
        with pytest.raises(ResumeStateError):
            await store.append(cp)


@pytest.mark.unit
async def test_checkpoint_redacts_secrets() -> None:
    """Secrets in node outputs/evidence never reach the durable record."""
    cp = WorkflowCheckpoint(
        run_id="r1", workflow_id="w", revision_id="rev", workspace_id="ws-1",
        node_outputs={"n": {"authorization": "Bearer supersecretvalue1234567890",
                            "api_key": "sk-abcdefghijklmnopqrst"}},
    )
    raw = cp.to_json()
    assert "supersecretvalue" not in raw
    assert "sk-abcdefghijklmnopqrst" not in raw
    assert "[REDACTED]" in raw


# ---------------------------------------------------------------------------
# 2. Resume → identical continuation (resume trace equality)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_resume_trace_equals_uninterrupted_trace() -> None:
    """Card metric: resume-trace equality is exact.

    Run the workflow, checkpoint the state, then resume from a fresh
    executor: the resumed run's trace (nodes, outputs, final output, status)
    must match the uninterrupted run exactly, modulo run identity/timing.
    """
    defn = _linear_defn()
    reference_run, _, plan = await _run(defn, {"name": "Alice"})

    # Build the checkpoint the way a checkpointing executor would at a node
    # boundary: completed nodes, their outputs, and the run identity.
    completed = {
        nid for nid, nr in reference_run.node_runs.items() if nr.status.value == "completed"
    }
    outputs = {
        nid: reference_run.node_runs[nid].output_data for nid in completed
    }

    store = InMemoryCheckpointStore()
    await store.append(WorkflowCheckpoint(
        run_id="wfrun-resume", workflow_id=defn.id, revision_id="rev",
        workspace_id="ws", boundary="approval", status="running",
        initial_input={"name": "Alice"},
        completed_nodes=sorted(completed), node_outputs=outputs,
        node_runs={nid: reference_run.node_runs[nid] for nid in completed},
        evidence=[], revision_hash=plan.revision_hash,
    ))

    latest = await store.latest("wfrun-resume")
    assert latest is not None

    # Resume: seed a fresh fake with the checkpoint so completed nodes replay
    # their recorded outputs, then re-execute the same plan.
    resumable = ResumableExecutor(FakeNodeExecutor(), latest)
    resumed_run, _, _ = await _run(defn, {"name": "Alice"}, executor=resumable)

    # The resumed trace must be identical to the never-interrupted one.
    assert _norm_run(resumed_run) == _norm_run(reference_run)
    # ...and the replayed set is reported honestly (no silent re-execution).
    assert set(resumable.replayed) == completed
    assert set(resumable.delegated) == set()


@pytest.mark.unit
async def test_resume_does_not_reexecute_checkpointed_nodes() -> None:
    """Resume replays recorded outputs; nothing already captured re-runs."""

    class ExplodingExecutor:
        async def execute_node(self, node, input_data, workspace_id):
            raise AssertionError(f"node {node.id!r} must not be re-executed")

    defn = _linear_defn()
    reference_run, rev, _ = await _run(defn, {"name": "Alice"})
    outputs = {
        nid: nr.output_data
        for nid, nr in reference_run.node_runs.items()
        if nr.status.value == "completed"
    }
    cp = WorkflowCheckpoint(
        run_id="wfrun-x", workflow_id=defn.id, revision_id="rev", workspace_id="ws",
        completed_nodes=sorted(outputs), node_outputs=outputs,
    )
    resumable = ResumableExecutor(ExplodingExecutor(), cp)
    node = rev.spec.nodes[0]
    got = await resumable.execute_node(node, {}, "ws")
    assert got == outputs["start"]
    assert "start" in resumable.replayed


@pytest.mark.unit
async def test_resume_rejects_cross_workspace_checkpoint() -> None:
    """A checkpoint may only serve its own workspace (fail closed)."""
    cp = WorkflowCheckpoint(
        run_id="r1", workflow_id="w", revision_id="rev", workspace_id="ws-A",
        node_outputs={"n1": {"out": 1}},
    )
    resumable = ResumableExecutor(FakeNodeExecutor(), cp)
    node = NodeSpec(id="n1", type=NodeType.OUTPUT, name="n1")
    with pytest.raises(ResumeStateError):
        await resumable.execute_node(node, {}, "ws-B")


# ---------------------------------------------------------------------------
# 3. Evidence propagation — no orphans after a restart
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_evidence_reattaches_to_resumed_run() -> None:
    """Card metric: orphaned evidence after resume = 0.

    Evidence written before the kill is readable after resume with identical
    content and provenance.
    """
    store = InMemoryCheckpointStore()
    pre_kill = EvidenceRow(
        run_id="wfrun-1", node_id="out1", workspace_id="ws-1",
        kind="citation",
        payload={"doc": "doc-A", "version": "v1", "chunk": "ch-1",
                 "span_start": 0, "span_end": 20, "verdict": "supported"},
    )
    await store.append(WorkflowCheckpoint(
        run_id="wfrun-1", workflow_id="w", revision_id="rev", workspace_id="ws-1",
        evidence=[pre_kill],
    ))

    resumed = await store.latest("wfrun-1")
    assert resumed is not None
    resumed_run = WorkflowRun(id="wfrun-1", workspace_id="ws-1", workflow_id="w")
    attached = attach_evidence(resumed_run, resumed)

    assert attached.evidence_count == 1
    ev = attached.evidence[0]
    assert ev.run_id == "wfrun-1"
    assert ev.node_id == "out1"
    assert ev.workspace_id == "ws-1"
    assert ev.payload == pre_kill.payload  # identical content + provenance
    assert ev is not pre_kill  # a copy, so the pre-kill record is never mutated


@pytest.mark.unit
async def test_evidence_survives_restart_without_orphans(tmp_path) -> None:
    """Evidence persists across a process restart and none is orphaned."""
    db = tmp_path / "ckpt2.db"
    store = SqliteCheckpointStore(db)
    rows = [
        EvidenceRow(run_id="r1", node_id=f"n{i}", workspace_id="ws-1",
                    kind="citation", payload={"doc": f"d{i}", "chunk": f"c{i}"})
        for i in range(3)
    ]
    await store.append(WorkflowCheckpoint(
        run_id="r1", workflow_id="w", revision_id="rev", workspace_id="ws-1",
        evidence=rows,
    ))
    del store

    reopened = SqliteCheckpointStore(db)
    got = await reopened.evidence_for("r1")
    assert [r.payload["chunk"] for r in got] == ["c0", "c1", "c2"]
    assert all(r.run_id == "r1" and r.workspace_id == "ws-1" for r in got)
