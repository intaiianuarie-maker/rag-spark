"""Unit tests for the W0/W1 workflow validator.

Tests duplicate/dangling IDs, cycles, required ports/outputs, conditional
edges, capability gating, and connectivity checks.  The validator is pure:
it never touches the filesystem or network.
"""

from __future__ import annotations

import pytest
from workflow_engine import (
    CapabilityManifest,
    NodeSpec,
    NodeType,
    PortSpec,
    WorkflowDefinition,
    WorkflowEdgeSpec,
    WorkflowValidator,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def full_manifest() -> CapabilityManifest:
    """A manifest that enables all node types and all standard capabilities."""
    return CapabilityManifest(
        supported_node_types=set(NodeType),
        enabled_capabilities={"llm", "retriever", "agent", "tool", "http"},
    )

@pytest.fixture
def no_capabilities_manifest() -> CapabilityManifest:
    """A manifest that supports all node types but no external capabilities."""
    return CapabilityManifest(
        supported_node_types=set(NodeType),
        enabled_capabilities=set(),
    )

def _make_nodes() -> list:
    return [
        NodeSpec(
            id="trigger",
            type=NodeType.TRIGGER,
            output_ports=[PortSpec(name="output", type="dict")],
        ),
        NodeSpec(
            id="transform",
            type=NodeType.TRANSFORM,
            config={"template": "Hi {name}!"},
            input_ports=[PortSpec(name="input", type="any")],
            output_ports=[PortSpec(name="output", type="string")],
        ),
        NodeSpec(
            id="output",
            type=NodeType.OUTPUT,
            input_ports=[PortSpec(name="input", type="any")],
            output_ports=[PortSpec(name="output", type="any")],
        ),
    ]

def _make_edges() -> list:
    return [
        WorkflowEdgeSpec(
            id="e1", source_node_id="trigger", source_port="output",
            target_node_id="transform", target_port="input",
        ),
        WorkflowEdgeSpec(
            id="e2", source_node_id="transform", source_port="output",
            target_node_id="output", target_port="input",
        ),
    ]

def _make_definition(**kwargs) -> WorkflowDefinition:
    """Build a valid linear WorkflowDefinition, with overrides."""
    defaults = dict(
        id="wf-test",
        workspace_id="ws-1",
        name="Test",
        nodes=_make_nodes(),
        edges=_make_edges(),
        workspace_capabilities={"llm"},
    )
    defaults.update(kwargs)
    return WorkflowDefinition(**defaults)


# ---------------------------------------------------------------------------
# Positive: valid workflow
# ---------------------------------------------------------------------------

def test_valid_workflow_passes(no_capabilities_manifest: CapabilityManifest) -> None:
    defn = _make_definition()
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    assert result.valid, f"Expected valid, got: {result.error_summary}"
    assert result.errors == []


def test_trigger_exempt_from_port_connectivity(
    no_capabilities_manifest: CapabilityManifest,
) -> None:
    """Trigger nodes receive initial_input, so required input ports are not
    required to be connected by edges."""
    node_trigger = NodeSpec(
        id="trigger",
        type=NodeType.TRIGGER,
        input_ports=[PortSpec(name="ctx", type="any")],
        output_ports=[PortSpec(name="output", type="dict")],
    )
    defn = WorkflowDefinition(
        id="wf-trigger-exempt",
        workspace_id="ws",
        name="Test",
        nodes=[
            node_trigger,
            NodeSpec(
                id="out",
                type=NodeType.OUTPUT,
                input_ports=[PortSpec(name="input", type="any")],
            ),
        ],
        edges=[
            WorkflowEdgeSpec(
                id="e1", source_node_id="trigger", source_port="output",
                target_node_id="out", target_port="input",
            ),
        ],
    )
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    assert result.valid, f"Expected valid: {result.error_summary}"


# ---------------------------------------------------------------------------
# Duplicate IDs
# ---------------------------------------------------------------------------

def test_duplicate_node_ids(no_capabilities_manifest: CapabilityManifest) -> None:
    nodes = [*_make_nodes(), NodeSpec(id="trigger", type=NodeType.TRANSFORM)]
    defn = _make_definition(nodes=nodes)
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    assert not result.valid
    dup_errors = [e for e in result.errors if e.code == "DUPLICATE_NODE_ID"]
    assert len(dup_errors) == 1
    assert dup_errors[0].node_id == "trigger"


def test_duplicate_edge_ids(no_capabilities_manifest: CapabilityManifest) -> None:
    edges = [*_make_edges(), WorkflowEdgeSpec(
        id="e1", source_node_id="transform", source_port="output",
        target_node_id="output", target_port="input",
    )]
    defn = _make_definition(edges=edges)
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    assert not result.valid
    dup_errors = [e for e in result.errors if e.code == "DUPLICATE_EDGE_ID"]
    assert len(dup_errors) == 1


# ---------------------------------------------------------------------------
# Dangling edges
# ---------------------------------------------------------------------------

def test_dangling_source_node(no_capabilities_manifest: CapabilityManifest) -> None:
    edges = [*_make_edges(), WorkflowEdgeSpec(
        id="e3", source_node_id="ghost", source_port="output",
        target_node_id="output", target_port="input",
    )]
    defn = _make_definition(edges=edges)
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    assert not result.valid
    errors = [e for e in result.errors if e.code == "DANGLING_SOURCE"]
    assert len(errors) == 1
    assert errors[0].edge_id == "e3"


def test_dangling_target_node(no_capabilities_manifest: CapabilityManifest) -> None:
    edges = [*_make_edges(), WorkflowEdgeSpec(
        id="e3", source_node_id="transform", source_port="output",
        target_node_id="ghost", target_port="input",
    )]
    defn = _make_definition(edges=edges)
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    assert not result.valid
    errors = [e for e in result.errors if e.code == "DANGLING_TARGET"]
    assert len(errors) == 1


def test_dangling_source_port(no_capabilities_manifest: CapabilityManifest) -> None:
    edges = [*_make_edges(), WorkflowEdgeSpec(
        id="e3", source_node_id="trigger", source_port="nonexistent",
        target_node_id="output", target_port="input",
    )]
    defn = _make_definition(edges=edges)
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    assert not result.valid
    errors = [e for e in result.errors if e.code == "DANGLING_SOURCE_PORT"]
    assert len(errors) == 1


def test_dangling_target_port(no_capabilities_manifest: CapabilityManifest) -> None:
    edges = [*_make_edges(), WorkflowEdgeSpec(
        id="e3", source_node_id="trigger", source_port="output",
        target_node_id="output", target_port="nonexistent",
    )]
    defn = _make_definition(edges=edges)
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    assert not result.valid
    errors = [e for e in result.errors if e.code == "DANGLING_TARGET_PORT"]
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------

def test_cycle_detected(no_capabilities_manifest: CapabilityManifest) -> None:
    defn = _make_definition(
        nodes=[
            NodeSpec(id="a", type=NodeType.TRIGGER, name="A",
                     output_ports=[PortSpec(name="out", type="any")]),
            NodeSpec(id="b", type=NodeType.TRANSFORM, name="B",
                     input_ports=[PortSpec(name="in", type="any")],
                     output_ports=[PortSpec(name="out", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="a", source_port="out",
                             target_node_id="b", target_port="in"),
            WorkflowEdgeSpec(id="e2", source_node_id="b", source_port="out",
                             target_node_id="a", target_port="in",
                             ),
        ],
    )
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    assert not result.valid
    cycle_errors = [e for e in result.errors if e.code == "CYCLE_DETECTED"]
    assert len(cycle_errors) >= 1


def test_self_loop_detected(no_capabilities_manifest: CapabilityManifest) -> None:
    defn = _make_definition(
        nodes=[
            NodeSpec(id="a", type=NodeType.TRIGGER, name="A",
                     output_ports=[PortSpec(name="out", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="a", source_port="out",
                             target_node_id="a", target_port="in",
                             ),
        ],
    )
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    assert not result.valid
    cycle_errors = [e for e in result.errors if e.code == "CYCLE_DETECTED"]
    assert len(cycle_errors) >= 1


# ---------------------------------------------------------------------------
# Capability gating
# ---------------------------------------------------------------------------

def test_unsupported_node_type(no_capabilities_manifest: CapabilityManifest) -> None:
    """HTTP node type not in manifest → unsupported."""
    restricted = CapabilityManifest(
        supported_node_types={NodeType.TRIGGER, NodeType.OUTPUT},
        enabled_capabilities=set(),
    )
    # We need at least trigger + output to avoid triggering OTHER errors.
    defn = WorkflowDefinition(
        id="wf-http",
        workspace_id="ws",
        name="HTTP Test",
        nodes=[
            NodeSpec(id="t", type=NodeType.TRIGGER, name="T",
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="h", type=NodeType.HTTP, name="HTTP",
                     input_ports=[PortSpec(name="input", type="any")]),
            NodeSpec(id="o", type=NodeType.OUTPUT, name="O",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="t", source_port="output",
                             target_node_id="h", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="h", source_port="output",
                             target_node_id="o", target_port="input",
                             condition_value=None),
        ],
    )
    result = WorkflowValidator.validate(defn, restricted)
    unsupported = [e for e in result.errors if e.code == "UNSUPPORTED_NODE_TYPE"]
    assert len(unsupported) == 1
    assert unsupported[0].node_id == "h"


def test_unsupported_capability(
    no_capabilities_manifest: CapabilityManifest,
) -> None:
    """Node requiring 'llm' capability but manifest doesn't enable it."""
    defn = _make_definition(
        nodes=[
            NodeSpec(id="trigger", type=NodeType.TRIGGER, name="T",
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="llm_node", type=NodeType.LLM, name="LLM",
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")],
                     required_capabilities=["llm"]),
            NodeSpec(id="out", type=NodeType.OUTPUT, name="O",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="trigger", source_port="output",
                             target_node_id="llm_node", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="llm_node", source_port="output",
                             target_node_id="out", target_port="input"),
        ],
    )
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    unsupported_caps = [e for e in result.errors if e.code == "UNSUPPORTED_CAPABILITY"]
    assert len(unsupported_caps) == 1
    assert unsupported_caps[0].node_id == "llm_node"


def test_unsupported_capability_passes_when_enabled(
    full_manifest: CapabilityManifest,
) -> None:
    defn = _make_definition(
        nodes=[
            NodeSpec(id="trigger", type=NodeType.TRIGGER, name="T",
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="llm_node", type=NodeType.LLM, name="LLM",
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")],
                     required_capabilities=["llm"]),
            NodeSpec(id="out", type=NodeType.OUTPUT, name="O",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="trigger", source_port="output",
                             target_node_id="llm_node", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="llm_node", source_port="output",
                             target_node_id="out", target_port="input"),
        ],
    )
    result = WorkflowValidator.validate(defn, full_manifest)
    assert result.valid, f"Expected valid: {result.error_summary}"


# ---------------------------------------------------------------------------
# Required ports / outputs
# ---------------------------------------------------------------------------

def test_required_input_port_not_connected(
    no_capabilities_manifest: CapabilityManifest,
) -> None:
    defn = _make_definition(
        nodes=[
            NodeSpec(id="trigger", type=NodeType.TRIGGER, name="T",
                     output_ports=[PortSpec(name="output", type="any")]),
            # transform has required input port 'input' but no edge connects to it
            NodeSpec(id="transform", type=NodeType.TRANSFORM, name="T2",
                     config={"template": "Hi"},
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="output", type=NodeType.OUTPUT, name="O",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="trigger", source_port="output",
                             target_node_id="transform", target_port="input"),
            # Missing edge: transform -> output
        ],
    )
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    port_errors = [e for e in result.errors if e.code == "UNCONNECTED_REQUIRED_INPUT"]
    assert len(port_errors) >= 1


def test_no_output_node(no_capabilities_manifest: CapabilityManifest) -> None:
    defn = _make_definition(
        nodes=[
            NodeSpec(id="trigger", type=NodeType.TRIGGER, name="T",
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="transform", type=NodeType.TRANSFORM, name="T2",
                     config={"template": "Hi"},
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="trigger", source_port="output",
                             target_node_id="transform", target_port="input"),
        ],
    )
    # Remove the output node by replacing with an orphaned transform
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    assert not result.valid
    output_errors = [e for e in result.errors if e.code == "NO_OUTPUT_NODE"]
    assert len(output_errors) == 1


def test_no_trigger_node(no_capabilities_manifest: CapabilityManifest) -> None:
    defn = _make_definition(
        nodes=[
            NodeSpec(id="transform", type=NodeType.TRANSFORM, name="T",
                     config={"template": "Hi"},
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="output", type=NodeType.OUTPUT, name="O",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="transform", source_port="output",
                             target_node_id="output", target_port="input"),
        ],
    )
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    trigger_errors = [e for e in result.errors if e.code == "NO_TRIGGER"]
    assert len(trigger_errors) == 1


def test_orphaned_node_detection(
    no_capabilities_manifest: CapabilityManifest,
) -> None:
    defn = _make_definition(
        nodes=[
            NodeSpec(id="trigger", type=NodeType.TRIGGER, name="T",
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="orphan", type=NodeType.TRANSFORM, name="Orphan",
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="output", type=NodeType.OUTPUT, name="O",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="trigger", source_port="output",
                             target_node_id="output", target_port="input"),
        ],
    )
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    orphaned = [e for e in result.errors if e.code == "ORPHANED_NODE"]
    assert len(orphaned) == 1
    assert orphaned[0].node_id == "orphan"


def test_empty_workflow(no_capabilities_manifest: CapabilityManifest) -> None:
    defn = WorkflowDefinition(
        id="empty", workspace_id="ws", name="Empty",
        nodes=[], edges=[],
    )
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    assert not result.valid
    empty_errors = [e for e in result.errors if e.code == "EMPTY_WORKFLOW"]
    assert len(empty_errors) == 1


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------

def test_conditional_edge_without_value(
    no_capabilities_manifest: CapabilityManifest,
) -> None:
    defn = _make_definition(
        nodes=[
            NodeSpec(id="trigger", type=NodeType.TRIGGER, name="T",
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="cond", type=NodeType.CONDITION, name="C",
                     config={"field": "x", "expected": "yes"},
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="result", type="boolean")]),
            NodeSpec(id="out", type=NodeType.OUTPUT, name="O",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="trigger", source_port="output",
                             target_node_id="cond", target_port="input"),
            # Missing condition_value on edge from CONDITION node
            WorkflowEdgeSpec(id="e2", source_node_id="cond", source_port="result",
                             target_node_id="out", target_port="input"),
        ],
    )
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    cond_errors = [e for e in result.errors if e.code == "CONDITION_EDGE_NO_VALUE"]
    assert len(cond_errors) == 1


# ---------------------------------------------------------------------------
# ValidationResult properties
# ---------------------------------------------------------------------------

def test_validation_result_error_summary(
    no_capabilities_manifest: CapabilityManifest,
) -> None:
    defn = _make_definition(
        nodes=[
            NodeSpec(id="n", type=NodeType.TRIGGER, name="N"),
        ],
    )
    result = WorkflowValidator.validate(defn, no_capabilities_manifest)
    assert not result.valid
    summary = result.error_summary
    assert "valid" not in summary  # not the "valid" string
    assert len(summary) > 0


def test_valid_result_summary(no_capabilities_manifest: CapabilityManifest) -> None:
    result = WorkflowValidator.validate(_make_definition(), no_capabilities_manifest)
    assert result.valid
    assert result.error_summary == "valid"
