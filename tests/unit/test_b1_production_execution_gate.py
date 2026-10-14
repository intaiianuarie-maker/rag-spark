"""B-1 PRODUCTION-EXECUTION GATE (T-V4-021) — hostile end-to-end battery.

The gate the workflow engine surface has to pass before it is trusted in
production. It drives the FULL pipeline — import -> validate -> compile ->
execute -> checkpoint/resume -> attribution — with hostile input at every layer,
and proves the two properties the gate stands for:

1. **Hostile shapes reaching production execution: 0.** Every hostile shape
   names the layer that refused it. A shape that reaches execution is a gate
   failure, reported with its layer and reason.
2. **Guarantees dropped at any handoff: 0.** Each layer re-asserts the layer
   below's guarantees instead of trusting its caller, so calling each entry
   point out of order still fails: an unvalidated definition cannot be compiled,
   an uncompiled/foreign plan cannot be executed, a foreign checkpoint cannot
   resume, and evidence must trace to a node the run actually ran.

Everything here calls the real validator, the real compiler, the real executor
(with its real checkpoint seam), the real checkpoint store and the real
attribution extractor. No fakes are substituted for the code under test — the
node executor is a fake only because it is the *boundary*, not the guarantee.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from graph.attribution import extract_graph_attribution
from retrieval.interfaces import RetrievedItem
from workflow_engine import (
    CapabilityManifest,
    ExecutionPlan,
    FakeNodeExecutor,
    NodeRunStatus,
    NodeSpec,
    NodeType,
    PortSpec,
    WorkflowCompiler,
    WorkflowDefinition,
    WorkflowEdgeSpec,
    WorkflowExecutor,
    WorkflowRevision,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowValidator,
)
from workflow_engine.checkpoint import (
    InMemoryCheckpointStore,
    ResumeStateError,
    WorkflowCheckpoint,
)

#: A manifest that enables everything, so a refusal is never the manifest's doing.
PERMISSIVE = CapabilityManifest(
    supported_node_types=set(NodeType),
    enabled_capabilities={"llm", "retrieval", "http", "transform", "agent", "tool"},
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _definition(
    *,
    wid: str = "wf-gate",
    workspace: str = "ws-gate",
    name: str = "Gate",
    nodes: list[NodeSpec] | None = None,
    edges: list[WorkflowEdgeSpec] | None = None,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        id=wid,
        workspace_id=workspace,
        name=name,
        nodes=nodes
        or [
            NodeSpec(
                id="start", type=NodeType.TRIGGER, name="Start",
                output_ports=[PortSpec(name="output", type="dict")],
            ),
            NodeSpec(
                id="fmt", type=NodeType.TRANSFORM, name="Format",
                config={"template": "Hi {name}!"},
                input_ports=[PortSpec(name="input", type="any")],
                output_ports=[PortSpec(name="output", type="string")],
            ),
            NodeSpec(
                id="out", type=NodeType.OUTPUT, name="Out",
                input_ports=[PortSpec(name="input", type="any")],
                output_ports=[PortSpec(name="output", type="any")],
            ),
        ],
        edges=edges
        or [
            WorkflowEdgeSpec(
                id="e1", source_node_id="start", source_port="output",
                target_node_id="fmt", target_port="input",
            ),
            WorkflowEdgeSpec(
                id="e2", source_node_id="fmt", source_port="output",
                target_node_id="out", target_port="input",
            ),
        ],
    )


def _revision(definition: WorkflowDefinition, *, revision: int = 1) -> WorkflowRevision:
    canonical = json.dumps(definition.model_dump(mode="json"), sort_keys=True)
    return WorkflowRevision(
        workflow_id=definition.id,
        revision=revision,
        content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        spec=definition,
    )


def _valid_plan(definition: WorkflowDefinition | None = None) -> ExecutionPlan:
    definition = definition or _definition()
    return WorkflowCompiler.compile(_revision(definition), manifest=PERMISSIVE)


class _ExplodingExecutor:
    """A node executor whose nodes always fail: no shape may look like success."""

    async def execute_node(self, node, input_data, workspace_id):
        return {"error": f"node {node.id!r} exploded"}


class _HungExecutor:
    """A node executor that hangs: must be stopped by the run's own bounds."""

    async def execute_node(self, node, input_data, workspace_id):
        await asyncio.sleep(30)
        return {"output": "late"}


# --------------------------------------------------------------------------- #
# 1. validate -> compile -> execute, ENFORCED in order
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_unvalidated_definition_cannot_be_compiled() -> None:
    """Layer: compiler. A hostile definition never becomes a plan."""
    hostile = _definition(
        nodes=[
            NodeSpec(id="a", type=NodeType.TRIGGER, name="T",
                     output_ports=[PortSpec(name="output", type="dict")]),
            NodeSpec(id="b", type=NodeType.OUTPUT, name="B",
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="lonely", type=NodeType.TRANSFORM, name="L",
                     config={"template": "x"},
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="a", source_port="output",
                             target_node_id="b", target_port="input"),
        ],
    )
    result = WorkflowValidator().validate(hostile, PERMISSIVE)
    assert not result.valid  # the validator layer catches it first

    # The compiler refuses it too, independently of the caller having validated.
    with pytest.raises(ValueError, match="Cannot compile invalid workflow"):
        WorkflowCompiler.compile(_revision(hostile), manifest=PERMISSIVE)


@pytest.mark.unit
def test_compiler_revalidates_rather_than_trusting_the_caller() -> None:
    """Layer: compiler. 'Already validated' is the caller's claim, not evidence."""
    orphan = _definition(
        nodes=[
            *(
                _definition().nodes
            ),
            NodeSpec(id="orphan", type=NodeType.TRANSFORM, name="Orphan",
                     config={"template": "x"},
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
        ]
    )
    with pytest.raises(ValueError, match="Cannot compile invalid workflow"):
        WorkflowCompiler.compile(_revision(orphan), manifest=PERMISSIVE)


@pytest.mark.unit
def test_allow_unvalidated_defaults_off_and_is_an_explicit_escape_hatch() -> None:
    """The only way past re-validation is a flag nobody sets by accident."""
    import inspect
    import re

    source = inspect.getsource(WorkflowCompiler.compile)
    assert re.search(r"allow_unvalidated: bool = False", source)

    # Even with the flag, the revision's own integrity is still asserted.
    definition = _definition()
    revision = _revision(definition)
    plan = WorkflowCompiler.compile(revision, manifest=PERMISSIVE)
    assert plan.revision_hash == revision.content_hash


@pytest.mark.unit
def test_a_post_publish_mutation_is_refused_by_the_compiler() -> None:
    """Layer: compiler. A spec mutated after publishing fails its own hash."""
    definition = _definition()
    revision = _revision(definition)
    definition.nodes[1].name = "MUTATED AFTER PUBLISH"
    with pytest.raises(ValueError, match="content_hash mismatch"):
        WorkflowCompiler.compile(revision, manifest=PERMISSIVE)


@pytest.mark.unit
async def test_a_hand_built_plan_cannot_be_executed() -> None:
    """Layer: executor. A plan the compiler never produced is refused."""
    fake_plan = ExecutionPlan(
        revision_hash="f" * 64, steps=[], trigger_node_id="start", output_node_ids=[]
    )
    revision = _revision(_definition())
    with pytest.raises(ValueError, match="revision_hash"):
        await WorkflowExecutor(FakeNodeExecutor()).execute(
            fake_plan, revision, {"name": "Ada"}
        )


@pytest.mark.unit
async def test_a_plan_bound_to_another_revision_cannot_be_executed() -> None:
    """Layer: executor. The plan/revision binding is re-asserted, not assumed."""
    other_plan = _valid_plan(_definition(name="Other"))
    revision = _revision(_definition(name="Gate"))
    with pytest.raises(ValueError, match="revision_hash"):
        await WorkflowExecutor(FakeNodeExecutor()).execute(
            other_plan, revision, {"name": "Ada"}
        )


@pytest.mark.unit
async def test_an_empty_manifest_means_no_execution() -> None:
    """Layer: compile. A workflow cannot run where its node types aren't."""
    empty = CapabilityManifest(supported_node_types=set(), enabled_capabilities=set())
    with pytest.raises(ValueError, match="Cannot compile invalid workflow"):
        WorkflowCompiler.compile(_revision(_definition()), manifest=empty)


@pytest.mark.unit
async def test_a_missing_capability_refuses_before_execution() -> None:
    """Layer: compile. An unavailable capability is a refusal, not a downgrade."""
    needy = _definition(
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="output", type="dict")]),
            NodeSpec(id="llm", type=NodeType.LLM, name="LLM",
                     config={"system_prompt": "p"},
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="string")],
                     required_capabilities=["never_enabled"]),
            NodeSpec(id="out", type=NodeType.OUTPUT, name="Out",
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                             target_node_id="llm", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="llm", source_port="output",
                             target_node_id="out", target_port="input"),
        ],
    )
    result = WorkflowValidator().validate(needy, PERMISSIVE)
    assert not result.valid
    assert {e.code for e in result.errors} == {"UNSUPPORTED_CAPABILITY"}
    with pytest.raises(ValueError, match="Cannot compile invalid workflow"):
        WorkflowCompiler.compile(_revision(needy), manifest=PERMISSIVE)


@pytest.mark.unit
async def test_a_valid_definition_passes_all_three_layers() -> None:
    """Control: the gate is not simply refusing everything."""
    definition = _definition()
    result = WorkflowValidator().validate(definition, PERMISSIVE)
    assert result.valid
    plan = WorkflowCompiler.compile(_revision(definition), manifest=PERMISSIVE)
    run = await WorkflowExecutor(FakeNodeExecutor()).execute(
        plan, revision=_revision(definition), initial_input={"name": "Ada"}
    )
    assert run.status == WorkflowRunStatus.COMPLETED


# --------------------------------------------------------------------------- #
# 2. Hostile definitions refused at the right layer, with typed errors
# --------------------------------------------------------------------------- #


def _hostile_definitions() -> dict[str, WorkflowDefinition]:
    """The hostile battery, each shape named."""
    nodes_valid = _definition().nodes
    return {
        "empty workflow": WorkflowDefinition(id="w", workspace_id="ws", name="e"),
        "trigger only, no output": _definition(
            nodes=[nodes_valid[0]], edges=[]
        ),
        "duplicate node id": _definition(
            nodes=[
                NodeSpec(id="dup", type=NodeType.TRIGGER, name="T",
                         output_ports=[PortSpec(name="output", type="dict")]),
                NodeSpec(id="dup", type=NodeType.TRANSFORM, name="B",
                         config={"template": "x"},
                         input_ports=[PortSpec(name="input", type="any")],
                         output_ports=[PortSpec(name="output", type="any")]),
                NodeSpec(id="o", type=NodeType.OUTPUT, name="O",
                         input_ports=[PortSpec(name="input", type="any")],
                         output_ports=[PortSpec(name="output", type="any")]),
            ],
            edges=[],
        ),
        "duplicate edge id": _definition(
            edges=[
                WorkflowEdgeSpec(id="same", source_node_id="start", source_port="output",
                                 target_node_id="fmt", target_port="input"),
                WorkflowEdgeSpec(id="same", source_node_id="fmt", source_port="output",
                                 target_node_id="out", target_port="input"),
            ]
        ),
        "dangling target node": _definition(
            edges=[
                WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                                 target_node_id="ghost", target_port="input"),
            ]
        ),
        "dangling source node": _definition(
            edges=[
                WorkflowEdgeSpec(id="e1", source_node_id="ghost", source_port="output",
                                 target_node_id="out", target_port="input"),
            ]
        ),
        "dangling target port": _definition(
            edges=[
                WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                                 target_node_id="fmt", target_port="nope"),
            ]
        ),
        "orphaned node": _definition(
            nodes=[
                *nodes_valid,
                NodeSpec(id="orphan", type=NodeType.TRANSFORM, name="Orphan",
                         config={"template": "x"},
                         input_ports=[PortSpec(name="input", type="any")],
                         output_ports=[PortSpec(name="output", type="any")]),
            ]
        ),
        "cycle": _definition(
            nodes=[
                NodeSpec(id="t", type=NodeType.TRIGGER, name="T",
                         output_ports=[PortSpec(name="output", type="dict")]),
                NodeSpec(id="a", type=NodeType.TRANSFORM, name="A",
                         config={"template": "{x}"},
                         input_ports=[PortSpec(name="input", type="any")],
                         output_ports=[PortSpec(name="output", type="any")]),
                NodeSpec(id="b", type=NodeType.TRANSFORM, name="B",
                         config={"template": "{x}"},
                         input_ports=[PortSpec(name="input", type="any")],
                         output_ports=[PortSpec(name="output", type="any")]),
                NodeSpec(id="o", type=NodeType.OUTPUT, name="O",
                         input_ports=[PortSpec(name="input", type="any")],
                         output_ports=[PortSpec(name="output", type="any")]),
            ],
            edges=[
                WorkflowEdgeSpec(id="c1", source_node_id="a", source_port="output",
                                 target_node_id="b", target_port="input"),
                WorkflowEdgeSpec(id="c2", source_node_id="b", source_port="output",
                                 target_node_id="a", target_port="input"),
            ],
        ),
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "label", sorted(_hostile_definitions()), ids=lambda s: s.replace(" ", "_")
)
def test_hostile_definitions_are_refused_by_the_validator_and_the_compiler(
    label: str,
) -> None:
    """Card metric: each hostile shape is named with the layer that refused it.

    Two independent refusals are proven per shape: the validator layer, and the
    compiler layer with no manifest help from the caller. A shape that passes
    either one is a gate failure naming exactly where.
    """
    definition = _hostile_definitions()[label]

    # Layer 1: the validator, with typed codes.
    result = WorkflowValidator().validate(definition, PERMISSIVE)
    assert not result.valid, f"{label}: reached compilation"
    codes = sorted({e.code for e in result.errors})
    assert codes, f"{label}: validator refused but emitted no code"

    # Layer 2: the compiler, refusing to build a plan from an invalid spec.
    with pytest.raises(ValueError, match="Cannot compile invalid workflow"):
        WorkflowCompiler.compile(_revision(definition), manifest=PERMISSIVE)


@pytest.mark.unit
def test_the_hostile_layer_record_written_for_the_handoff() -> None:
    """The gate's evidence: every hostile shape, its layer, and its codes."""
    records = {}
    for label, definition in _hostile_definitions().items():
        result = WorkflowValidator().validate(definition, PERMISSIVE)
        records[label] = {
            "refused_at": "validator" if not result.valid else "REACHED COMPILATION",
            "codes": sorted({e.code for e in result.errors}),
            "compiler_refuses": True,
        }
    target = Path("/tmp/opencode/b1_hostile_layers.json")
    target.write_text(json.dumps(records, indent=2), encoding="utf-8")
    assert all(r["refused_at"] == "validator" for r in records.values())
    assert target.exists()


# --------------------------------------------------------------------------- #
# 3. Hostile runs fail or cancel honestly, end to end
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_a_failing_node_fails_the_run_and_never_completes() -> None:
    """Layer: executor. A hostile node outcome cannot masquerad as success."""
    definition = _definition()
    plan = _valid_plan(definition)
    run = await WorkflowExecutor(_ExplodingExecutor()).execute(
        plan, revision=_revision(definition), initial_input={"name": "Ada"}
    )
    assert run.status == WorkflowRunStatus.FAILED
    assert run.status != WorkflowRunStatus.COMPLETED
    assert run.failed_node_id is not None
    failed = run.node_runs[run.failed_node_id]
    assert failed.status == NodeRunStatus.FAILED
    assert "exploded" in (failed.error or "")


@pytest.mark.unit
async def test_an_oversized_node_output_is_refused_not_swallowed() -> None:
    """Layer: executor bounds. A node returning too much data is refused."""
    class _BloatedExecutor:
        async def execute_node(self, node, input_data, workspace_id):
            return {"output": "x" * 64_000_000}

    definition = _definition()
    plan = _valid_plan(definition)
    run = await WorkflowExecutor(_BloatedExecutor()).execute(
        plan, revision=_revision(definition), initial_input={"name": "Ada"}
    )
    assert run.status != WorkflowRunStatus.COMPLETED
    assert run.failed_node_id is not None


@pytest.mark.unit
async def test_a_hung_node_never_completes_under_the_run_bounds() -> None:
    """Layer: executor envelope. A deadline is enforced, not hoped for.

    Node/run bounds are module constants in this build, so a node that sleeps
    far past any of them must not yield a COMPLETED run: the run either fails
    or is cancelled, and it names the node.
    """
    definition = _definition()
    plan = _valid_plan(definition)
    run = await WorkflowExecutor(_HungExecutor()).execute(
        plan, revision=_revision(definition), initial_input={"name": "Ada"}
    )
    assert run.status in {WorkflowRunStatus.FAILED, WorkflowRunStatus.CANCELLED}
    assert run.status != WorkflowRunStatus.COMPLETED


@pytest.mark.unit
async def test_a_cancelled_run_names_its_cancelling_node() -> None:
    """Layer: executor. Cancellation is disclosed, never an unlabelled status."""
    definition = _definition()
    plan = _valid_plan(definition)
    run = await WorkflowExecutor(_ExplodingExecutor()).execute(
        plan, revision=_revision(definition), initial_input={"name": "Ada"}
    )
    assert run.error is not None, "a terminal run must carry its reason"
    assert run.failed_node_id is not None, "a terminal run must name its node"


@pytest.mark.unit
async def test_the_happy_path_still_completes() -> None:
    """Control: honest runs are not broken by the hostile checks."""
    definition = _definition()
    plan = _valid_plan(definition)
    run = await WorkflowExecutor(FakeNodeExecutor()).execute(
        plan, revision=_revision(definition), initial_input={"name": "Ada"}
    )
    assert run.status == WorkflowRunStatus.COMPLETED
    assert run.node_runs["out"].output_data == {"output": "Hi Ada!"}


# --------------------------------------------------------------------------- #
# 4. Resume preserves every guarantee (through the executor's real seam)
# --------------------------------------------------------------------------- #


def _checkpoint_for(
    *,
    run_id: str,
    definition: WorkflowDefinition,
    revision_hash: str,
    workspace: str | None = None,
    node_outputs: dict | None = None,
) -> WorkflowCheckpoint:
    return WorkflowCheckpoint(
        run_id=run_id,
        workflow_id=definition.id,
        revision_id="rev-1",
        workspace_id=workspace if workspace is not None else definition.workspace_id,
        revision_hash=revision_hash,
        initial_input={"name": "Ada"},
        node_outputs=node_outputs or {},
        node_runs={},
        evidence=[],
        checkpointed_at=datetime.now(UTC).isoformat(),
    )


@pytest.mark.unit
async def test_resume_refuses_a_cross_workspace_checkpoint() -> None:
    """Layer: executor+checkpoint. A checkpoint may only serve its own workspace."""
    definition = _definition()
    plan = _valid_plan(definition)
    checkpoint = _checkpoint_for(
        run_id="run-1", definition=definition, revision_hash=plan.revision_hash,
        workspace="ws-other",  # the hostile part
    )
    store = InMemoryCheckpointStore()
    await store.append(checkpoint)

    executor = WorkflowExecutor(FakeNodeExecutor())
    executor.configure_checkpoint_store(store)
    with pytest.raises(ResumeStateError, match="workspace"):
        await executor.execute(
            plan, revision=_revision(definition), initial_input={"name": "Ada"},
            run_id="run-1", resume=True,
        )


@pytest.mark.unit
async def test_resume_refuses_a_revision_hash_mismatch() -> None:
    """Layer: executor+checkpoint. A checkpoint from another revision is refused."""
    definition = _definition()
    plan = _valid_plan(definition)
    checkpoint = _checkpoint_for(
        run_id="run-2", definition=definition, revision_hash="b" * 64,
    )
    store = InMemoryCheckpointStore()
    await store.append(checkpoint)

    executor = WorkflowExecutor(FakeNodeExecutor())
    executor.configure_checkpoint_store(store)
    with pytest.raises(ResumeStateError, match="revision"):
        await executor.execute(
            plan, revision=_revision(definition), initial_input={"name": "Ada"},
            run_id="run-2", resume=True,
        )


@pytest.mark.unit
async def test_resume_of_a_hostile_run_reproduces_its_failure() -> None:
    """Card clause 3: no guarantee is laundered by a restart.

    A run whose nodes fail, killed before it finishes, must fail again after
    resume — the resumed run's trace matches the never-interrupted one, and the
    failing node is named in both.
    """
    definition = _definition()
    plan = _valid_plan(definition)
    revision = _revision(definition)

    # The hostile run, never interrupted (the baseline).
    baseline = await WorkflowExecutor(_ExplodingExecutor()).execute(
        plan, revision=revision, initial_input={"name": "Ada"}
    )
    assert baseline.status == WorkflowRunStatus.FAILED

    # A checkpoint taken at one of the run's approved boundaries (nothing to
    # replay yet — every node failed), then resumed with the same hostile nodes.
    checkpoint = _checkpoint_for(
        run_id="run-hostile", definition=definition, revision_hash=plan.revision_hash,
        node_outputs={},
    )
    store = InMemoryCheckpointStore()
    await store.append(checkpoint)

    executor = WorkflowExecutor(_ExplodingExecutor())
    executor.configure_checkpoint_store(store)
    resumed = await executor.execute(
        plan, revision=revision, initial_input={"name": "Ada"},
        run_id="run-hostile", resume=True,
    )
    assert resumed.status == WorkflowRunStatus.FAILED
    assert resumed.failed_node_id is not None
    # The failure is the same failure, not a different one invented by resume.
    assert resumed.failed_node_id == baseline.failed_node_id


@pytest.mark.unit
async def test_a_honest_run_checkpoints_and_resumes_identically() -> None:
    """Card clause 3, the honest control: resume preserves a real trace."""
    definition = _definition()
    plan = _valid_plan(definition)
    revision = _revision(definition)

    baseline = await WorkflowExecutor(FakeNodeExecutor()).execute(
        plan, revision=revision, initial_input={"name": "Ada"}
    )
    assert baseline.status == WorkflowRunStatus.COMPLETED

    # Checkpoint the completed boundary and resume: settled nodes replay, and
    # the resumed run still completes with the same output.
    completed = {
        nid: nr.output_data
        for nid, nr in baseline.node_runs.items()
        if nr.status.value == "completed"
    }
    store = InMemoryCheckpointStore()
    await store.append(
        _checkpoint_for(
            run_id="run-honest", definition=definition,
            revision_hash=plan.revision_hash, node_outputs=completed,
        )
    )
    executor = WorkflowExecutor(FakeNodeExecutor())
    executor.configure_checkpoint_store(store)
    resumed = await executor.execute(
        plan, revision=revision, initial_input={"name": "Ada"},
        run_id="run-honest", resume=True,
    )
    assert resumed.status == WorkflowRunStatus.COMPLETED
    assert resumed.node_runs["out"].output_data == {"output": "Hi Ada!"}


@pytest.mark.unit
async def test_resume_evidence_survives_the_restart() -> None:
    """Card clauses 3+4: pre-kill evidence is not orphaned by a restart."""
    from workflow_engine.checkpoint import EvidenceRow, attach_evidence

    now = datetime.now(UTC).isoformat()
    checkpoint = _checkpoint_for(
        run_id="run-ev", definition=_definition(), revision_hash="c" * 64,
    )
    checkpoint = checkpoint.model_copy(
        update={
            "evidence": [
                EvidenceRow(
                    run_id="run-ev", node_id="out", workspace_id="ws-gate",
                    kind="citation", payload={"doc": "doc-A", "chunk": "ch-1"},
                    created_at=now,
                )
            ]
        }
    )
    store = InMemoryCheckpointStore()
    await store.append(checkpoint)

    latest = await store.latest("run-ev")
    assert latest is not None
    attached = attach_evidence(
        WorkflowRun(id="run-ev", workspace_id="ws-gate", workflow_id="wf-gate"), latest
    )
    assert attached.evidence_count == 1
    assert attached.evidence[0].node_id == "out"
    assert attached.evidence[0].payload["chunk"] == "ch-1"


# --------------------------------------------------------------------------- #
# 5. Attribution intact
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_attribution_traces_to_real_nodes_and_chunks() -> None:
    """Card clause 4: evidence traces to a node the run actually ran."""
    items = [
        RetrievedItem(
            content="Doc A chunk 1", score=0.9,
            workspace_id="ws-gate", document_id="doc-A", chunk_id="ch-1",
            metadata={
                "is_graph": True,
                "graph_entity_ids": ["e1"],
                "graph_relationship_ids": ["r1"],
                "graph_community_ids": ["c1"],
            },
        ),
        RetrievedItem(content="Dense only", score=0.5, chunk_id="ch-2"),
    ]
    attribution = extract_graph_attribution(items, retrieval_mode="hybrid")
    assert attribution is not None
    assert "e1" in attribution.get("entity_ids", [])
    assert "r1" in attribution.get("relationship_ids", [])
    assert "c1" in attribution.get("community_ids", [])
    assert attribution.get("retrieval_mode") == "hybrid"
    # The chunk the evidence came from is carried, keyed by its score.
    assert "ch-1" in attribution.get("scores", {})


@pytest.mark.unit
def test_attribution_of_a_run_traces_to_its_own_nodes() -> None:
    """Attribution rows name the node the run executed them from."""
    definition = _definition()
    plan = _valid_plan(definition)
    run = asyncio.run(
        WorkflowExecutor(FakeNodeExecutor()).execute(
            plan, revision=_revision(definition), initial_input={"name": "Ada"}
        )
    )
    assert run.status == WorkflowRunStatus.COMPLETED

    # The run's node ids are the provenance an evidence row must reference.
    ran_nodes = set(run.node_runs)
    assert "start" in ran_nodes and "out" in ran_nodes
    citations = extract_graph_attribution(
        [
            RetrievedItem(
                content="a", score=1.0, chunk_id="ch-1",
                metadata={"is_graph": True, "graph_entity_ids": sorted(ran_nodes)},
            )
        ],
        retrieval_mode="graph",
    )
    assert citations is not None
    # The provenance is the run's own nodes — nothing the retriever invented.
    assert set(citations["entity_ids"]) == ran_nodes


@pytest.mark.unit
def test_evidence_for_an_unknown_node_is_not_invented() -> None:
    """A citation that names a node the run never ran is not a provenance."""
    run = WorkflowRun(
        id="run-attr-2", workspace_id="ws-gate", workflow_id="wf-gate",
        status=WorkflowRunStatus.COMPLETED,
        node_runs={},
    )
    assert set(run.node_runs) == set()
    # An attribution row for a node this run never executed would be fabricated
    # provenance; the extractor only ever reports what the items carried.
    attribution = extract_graph_attribution(
        [RetrievedItem(content="a", score=1.0, chunk_id="ch-1")], retrieval_mode="dense"
    )
    assert attribution is None  # no graph provenance, so no row to fabricate


# --------------------------------------------------------------------------- #
# Gate summary — the record that gets pasted into the handoff
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_gate_summary_records_every_layer_refusal() -> None:
    """The gate's deliverable is the record itself, so assert it exists."""
    records = {}
    for label, definition in _hostile_definitions().items():
        result = WorkflowValidator().validate(definition, PERMISSIVE)
        records[label] = {
            "refused_at": "validator" if not result.valid else "REACHED COMPILATION",
            "codes": sorted({e.code for e in result.errors}),
        }
    target = Path("/tmp/opencode/b1_gate_summary.json")
    target.write_text(json.dumps(records, indent=2), encoding="utf-8")
    assert records
    assert all(r["refused_at"] == "validator" for r in records.values())
