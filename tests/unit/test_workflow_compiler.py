"""Unit tests for the W0/W1 deterministic workflow compiler.

Tests topological ordering, join-mode detection, determinism (same revision
→ same plan), no compile-time effects (no mutation of input), and rejection
of invalid workflows.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from workflow_engine import (
    CapabilityManifest,
    NodeSpec,
    NodeType,
    PortSpec,
    WorkflowCompiler,
    WorkflowDefinition,
    WorkflowEdgeSpec,
    WorkflowRevision,
)


def _manifest() -> CapabilityManifest:
    return CapabilityManifest(
        supported_node_types=set(NodeType),
        enabled_capabilities=set(),
    )


def _make_revision(defn: WorkflowDefinition) -> WorkflowRevision:
    canonical = json.dumps(defn.model_dump(mode="json"), sort_keys=True)
    h = hashlib.sha256(canonical.encode()).hexdigest()
    return WorkflowRevision(
        workflow_id=defn.id,
        revision=1,
        content_hash=h,
        spec=defn,
    )


def _linear_defn() -> WorkflowDefinition:
    return WorkflowDefinition(
        id="wf-linear",
        workspace_id="ws",
        name="Linear",
        nodes=[
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
                id="end", type=NodeType.OUTPUT, name="End",
                input_ports=[PortSpec(name="input", type="any")],
                output_ports=[PortSpec(name="output", type="any")],
            ),
        ],
        edges=[
            WorkflowEdgeSpec(
                id="e1", source_node_id="start", source_port="output",
                target_node_id="fmt", target_port="input",
            ),
            WorkflowEdgeSpec(
                id="e2", source_node_id="fmt", source_port="output",
                target_node_id="end", target_port="input",
            ),
        ],
    )


def _branch_defn() -> WorkflowDefinition:
    return WorkflowDefinition(
        id="wf-branch",
        workspace_id="ws",
        name="Branch",
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="output", type="dict")]),
            NodeSpec(id="cond", type=NodeType.CONDITION, name="Check",
                     config={"field": "doc_type", "expected": "legal"},
                     input_ports=[PortSpec(name="input", type="dict")],
                     output_ports=[PortSpec(name="result", type="boolean")]),
            NodeSpec(id="legal_out", type=NodeType.OUTPUT, name="Legal",
                     input_ports=[PortSpec(name="input", type="any")]),
            NodeSpec(id="general_out", type=NodeType.OUTPUT, name="General",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                             target_node_id="cond", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="cond", source_port="result",
                             target_node_id="legal_out", target_port="input",
                             condition_value=True),
            WorkflowEdgeSpec(id="e3", source_node_id="cond", source_port="result",
                             target_node_id="general_out", target_port="input",
                             condition_value=False),
        ],
    )


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------

def test_compile_linear_workflow() -> None:
    defn = _linear_defn()
    rev = _make_revision(defn)
    plan = WorkflowCompiler.compile(rev, allow_unvalidated=True)

    assert plan.revision_hash == rev.content_hash
    assert plan.trigger_node_id == "start"
    assert plan.output_node_ids == ["end"]
    assert len(plan.steps) == 3
    # Topological order: start, fmt, end
    order = [s.node_id for s in plan.steps]
    assert order == ["start", "fmt", "end"]


def test_compile_preserves_topological_order() -> None:
    """The compiler must produce a valid topological ordering."""
    defn = _linear_defn()
    rev = _make_revision(defn)
    plan = WorkflowCompiler.compile(rev, allow_unvalidated=True)

    # For each edge, source must come before target in the order.
    node_order = {s.node_id: s.order for s in plan.steps}
    for edge in defn.edges:
        assert node_order[edge.source_node_id] < node_order[edge.target_node_id], \
            f"Edge {edge.id}: source before target violated"


def test_compile_outgoing_edges_populated() -> None:
    defn = _branch_defn()
    rev = _make_revision(defn)
    plan = WorkflowCompiler.compile(rev, allow_unvalidated=True)

    cond_step = plan.step_by_node_id("cond")
    assert cond_step is not None
    assert len(cond_step.outgoing_edges) == 2
    edge_ids = {e.target_node_id for e in cond_step.outgoing_edges}
    assert edge_ids == {"legal_out", "general_out"}
    cond_vals = {e.condition_value for e in cond_step.outgoing_edges}
    assert cond_vals == {True, False}


def test_compile_join_mode_any_for_condition_downstream() -> None:
    """Nodes downstream of a CONDITION node should have join_mode='any'."""
    defn = _branch_defn()
    rev = _make_revision(defn)
    plan = WorkflowCompiler.compile(rev, allow_unvalidated=True)

    legal_step = plan.step_by_node_id("legal_out")
    assert legal_step is not None
    assert legal_step.join_mode == "any"

    general_step = plan.step_by_node_id("general_out")
    assert general_step is not None
    assert general_step.join_mode == "any"


def test_compile_join_mode_all_for_regular_merge() -> None:
    """A merge point (two unconditional edges into one node) should have
    join_mode='all'."""
    defn = WorkflowDefinition(
        id="wf-merge",
        workspace_id="ws",
        name="Merge",
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="out", type="any")]),
            NodeSpec(id="a", type=NodeType.TRANSFORM, name="A",
                     input_ports=[PortSpec(name="in", type="any")],
                     output_ports=[PortSpec(name="out", type="any")]),
            NodeSpec(id="b", type=NodeType.TRANSFORM, name="B",
                     input_ports=[PortSpec(name="in", type="any")],
                     output_ports=[PortSpec(name="out", type="any")]),
            NodeSpec(id="merge", type=NodeType.TRANSFORM, name="Merge",
                     config={"key": "out"},
                     input_ports=[PortSpec(name="a", type="any"), PortSpec(name="b", type="any")],
                     output_ports=[PortSpec(name="out", type="any")]),
            NodeSpec(id="end", type=NodeType.OUTPUT, name="End",
                     input_ports=[PortSpec(name="in", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="out",
                             target_node_id="a", target_port="in"),
            WorkflowEdgeSpec(id="e2", source_node_id="start", source_port="out",
                             target_node_id="b", target_port="in"),
            WorkflowEdgeSpec(id="e3", source_node_id="a", source_port="out",
                             target_node_id="merge", target_port="a"),
            WorkflowEdgeSpec(id="e4", source_node_id="b", source_port="out",
                             target_node_id="merge", target_port="b"),
            WorkflowEdgeSpec(id="e5", source_node_id="merge", source_port="out",
                             target_node_id="end", target_port="in"),
        ],
    )
    rev = _make_revision(defn)
    plan = WorkflowCompiler.compile(rev, allow_unvalidated=True)

    merge_step = plan.step_by_node_id("merge")
    assert merge_step is not None
    assert merge_step.join_mode == "all"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_compile_is_deterministic() -> None:
    """Compiling the same revision twice must produce identical plans."""
    defn = _branch_defn()
    rev = _make_revision(defn)

    plan1 = WorkflowCompiler.compile(rev, allow_unvalidated=True)
    plan2 = WorkflowCompiler.compile(rev, allow_unvalidated=True)

    assert plan1.steps == plan2.steps
    assert plan1.revision_hash == plan2.revision_hash
    assert plan1.trigger_node_id == plan2.trigger_node_id
    assert plan1.output_node_ids == plan2.output_node_ids


def test_compile_idempotent_across_revisions_same_spec() -> None:
    """Two revisions with the same spec must have the same content hash."""
    defn = _linear_defn()
    rev1 = _make_revision(defn)
    rev2 = _make_revision(defn)

    assert rev1.content_hash == rev2.content_hash

    plan1 = WorkflowCompiler.compile(rev1, allow_unvalidated=True)
    plan2 = WorkflowCompiler.compile(rev2, allow_unvalidated=True)

    assert plan1.steps == plan2.steps


# ---------------------------------------------------------------------------
# No compile-time effects
# ---------------------------------------------------------------------------

def test_compiler_does_not_mutate_revision() -> None:
    """The compiler must not mutate the input revision or its spec."""
    defn = _linear_defn()
    rev = _make_revision(defn)
    original_dump = rev.model_dump(mode="json")

    WorkflowCompiler.compile(rev, allow_unvalidated=True)

    assert rev.model_dump(mode="json") == original_dump


def test_compiler_no_filesystem_side_effects(
    tmp_path: pytest.fixture,
) -> None:
    """The compiler must not create files or directories."""
    defn = _linear_defn()
    rev = _make_revision(defn)

    before = {p.name for p in tmp_path.iterdir()}
    WorkflowCompiler.compile(rev, allow_unvalidated=True)
    after = {p.name for p in tmp_path.iterdir()}

    assert before == after


# ---------------------------------------------------------------------------
# Invalid workflows
# ---------------------------------------------------------------------------

def test_compile_rejects_invalid_workflow() -> None:
    """Compiling an invalid workflow raises ValueError."""
    invalid_defn = WorkflowDefinition(
        id="invalid",
        workspace_id="ws",
        name="Invalid",
        nodes=[
            NodeSpec(id="a", type=NodeType.TRIGGER, name="A"),
            NodeSpec(id="b", type=NodeType.TRANSFORM, name="B"),
        ],
        edges=[],
    )
    rev = _make_revision(invalid_defn)
    with pytest.raises(ValueError, match="Cannot compile invalid workflow"):
        WorkflowCompiler.compile(rev)


def test_compile_rejects_cycle() -> None:
    defn = WorkflowDefinition(
        id="cyclic",
        workspace_id="ws",
        name="Cyclic",
        nodes=[
            NodeSpec(id="a", type=NodeType.TRIGGER, name="A",
                     output_ports=[PortSpec(name="out", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="a", source_port="out",
                             target_node_id="a", target_port="in"),
        ],
    )
    rev = _make_revision(defn)
    with pytest.raises(ValueError):
        WorkflowCompiler.compile(rev, allow_unvalidated=True)


def test_compile_rejects_duplicate_node_ids() -> None:
    defn = WorkflowDefinition(
        id="dup",
        workspace_id="ws",
        name="Dup",
        nodes=[
            NodeSpec(id="a", type=NodeType.TRIGGER, name="A",
                     output_ports=[PortSpec(name="out", type="any")]),
            NodeSpec(id="a", type=NodeType.OUTPUT, name="A2",
                     input_ports=[PortSpec(name="in", type="any")]),
        ],
        edges=[],
    )
    rev = _make_revision(defn)
    with pytest.raises(ValueError, match="Cannot compile invalid workflow"):
        WorkflowCompiler.compile(rev)
