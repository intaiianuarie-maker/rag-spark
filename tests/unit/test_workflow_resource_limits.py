"""T-V4-006 — validator resource bounds: graph size, fan-out, config size.

Every cap is stated in ``workflow_engine.validator`` with its reason; this
module proves the three hostile shapes are refused fast (never OOM, never
hang) with typed rejection codes, that the caps sit at the ONE enforcement
point every definition passes (``WorkflowValidator.validate`` — and via it
``WorkflowCompiler.compile``, which re-validates), and that at-limit values
still pass (no over-rejection).

Shapes proven refused:
  1. a 10k-node graph            → TOO_MANY_NODES
  2. a 1:M fan-out hub           → NODE_FAN_OUT_TOO_HIGH
  3. a multi-MB config           → NODE_CONFIG_TOO_LARGE / GRAPH_CONFIG_TOO_LARGE
"""

from __future__ import annotations

import time

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
    WorkflowValidator,
)
from workflow_engine.validator import (
    MAX_CONFIG_BYTES_PER_NODE,
    MAX_CONFIG_BYTES_TOTAL,
    MAX_EDGES_PER_NODE,
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
)

#: Explicit refusal-time bound.  The pre-pass is O(n + m); these shapes must
#: refuse in milliseconds, so one second is a generous, non-flaky ceiling.
REFUSAL_TIME_LIMIT_S = 1.0


@pytest.fixture
def manifest() -> CapabilityManifest:
    return CapabilityManifest(
        supported_node_types=set(NodeType),
        enabled_capabilities={"llm", "retriever", "agent", "tool", "http"},
    )


def _node(node_id: str, node_type: NodeType, *, config: dict | None = None) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        type=node_type,
        config=config or {},
        input_ports=[PortSpec(name="input", type="any")],
        output_ports=[PortSpec(name="output", type="any")],
    )


def _edge(edge_id: str, source: str, target: str) -> WorkflowEdgeSpec:
    return WorkflowEdgeSpec(
        id=edge_id,
        source_node_id=source,
        source_port="output",
        target_node_id=target,
        target_port="input",
    )


def _definition(nodes: list[NodeSpec], edges: list[WorkflowEdgeSpec]) -> WorkflowDefinition:
    return WorkflowDefinition(
        id="wf-hostile",
        workspace_id="ws-1",
        name="Hostile",
        nodes=nodes,
        edges=edges,
    )


def _codes(result) -> set[str]:
    return {e.code for e in result.errors}


# ---------------------------------------------------------------------------
# Shape 1 — oversized node count
# ---------------------------------------------------------------------------


class TestGraphNodeCountBound:
    def test_10k_node_graph_refused_with_typed_code(self, manifest: CapabilityManifest) -> None:
        nodes = [_node(f"n{i}", NodeType.TRANSFORM) for i in range(10_000)]
        started = time.perf_counter()
        result = WorkflowValidator.validate(_definition(nodes, []), manifest)
        elapsed = time.perf_counter() - started
        assert result.valid is False
        assert "TOO_MANY_NODES" in _codes(result)
        assert elapsed < REFUSAL_TIME_LIMIT_S, f"refusal took {elapsed:.3f}s"

    def test_node_count_boundary_accepted(self, manifest: CapabilityManifest) -> None:
        """At-limit node count is not a resource error (other checks may fire)."""
        nodes = [_node(f"n{i}", NodeType.TRANSFORM) for i in range(MAX_GRAPH_NODES - 1)]
        nodes.append(_node("out", NodeType.OUTPUT))
        assert len(nodes) == MAX_GRAPH_NODES
        result = WorkflowValidator.validate(_definition(nodes, []), manifest)
        assert "TOO_MANY_NODES" not in _codes(result)

    def test_one_over_node_count_refused(self, manifest: CapabilityManifest) -> None:
        nodes = [_node(f"n{i}", NodeType.TRIGGER) for i in range(MAX_GRAPH_NODES + 1)]
        nodes.append(_node("out", NodeType.OUTPUT))
        result = WorkflowValidator.validate(_definition(nodes, []), manifest)
        assert "TOO_MANY_NODES" in _codes(result)

    def test_edge_count_bound(self, manifest: CapabilityManifest) -> None:
        """Total edge cap: a 10k-edge graph is refused even spread thin."""
        hub = _node("hub", NodeType.TRIGGER)
        targets = [_node(f"t{i}", NodeType.OUTPUT) for i in range(MAX_GRAPH_EDGES + 1)]
        edges = [_edge(f"e{i}", "hub", f"t{i}") for i in range(MAX_GRAPH_EDGES + 1)]
        started = time.perf_counter()
        result = WorkflowValidator.validate(_definition([hub, *targets], edges), manifest)
        elapsed = time.perf_counter() - started
        assert "TOO_MANY_EDGES" in _codes(result)
        assert elapsed < REFUSAL_TIME_LIMIT_S


# ---------------------------------------------------------------------------
# Shape 2 — per-node fan-out hub (1:M)
# ---------------------------------------------------------------------------


class TestFanOutBound:
    def test_fan_out_hub_refused_with_typed_code(self, manifest: CapabilityManifest) -> None:
        """A hub with 10k outgoing edges refuses in one pass, bounded."""
        hub = _node("hub", NodeType.TRIGGER)
        targets = [_node(f"t{i}", NodeType.OUTPUT) for i in range(64)]
        edges = [
            _edge(f"e{i % 64}", "hub", f"t{i % 64}") for i in range(10_000)
        ]
        started = time.perf_counter()
        result = WorkflowValidator.validate(_definition([hub, *targets], edges), manifest)
        elapsed = time.perf_counter() - started
        assert result.valid is False
        fan_errors = [e for e in result.errors if e.code == "NODE_FAN_OUT_TOO_HIGH"]
        assert fan_errors, f"expected fan-out refusal, got {_codes(result)}"
        assert fan_errors[0].node_id == "hub"
        assert elapsed < REFUSAL_TIME_LIMIT_S

    def test_fan_out_boundary_accepted(self, manifest: CapabilityManifest) -> None:
        """Exactly MAX_EDGES_PER_NODE outgoing edges is not a resource error."""
        hub = _node("hub", NodeType.TRIGGER)
        targets = [_node(f"t{i}", NodeType.OUTPUT) for i in range(MAX_EDGES_PER_NODE)]
        edges = [_edge(f"e{i}", "hub", f"t{i}") for i in range(MAX_EDGES_PER_NODE)]
        result = WorkflowValidator.validate(_definition([hub, *targets], edges), manifest)
        assert "NODE_FAN_OUT_TOO_HIGH" not in _codes(result)

    def test_fan_out_one_over_refused(self, manifest: CapabilityManifest) -> None:
        hub = _node("hub", NodeType.TRIGGER)
        targets = [_node(f"t{i}", NodeType.OUTPUT) for i in range(MAX_EDGES_PER_NODE + 1)]
        edges = [
            _edge(f"e{i}", "hub", f"t{i}")
            for i in range(MAX_EDGES_PER_NODE + 1)
        ]
        result = WorkflowValidator.validate(_definition([hub, *targets], edges), manifest)
        assert "NODE_FAN_OUT_TOO_HIGH" in _codes(result)

    def test_fan_out_message_echo_bounded(self, manifest: CapabilityManifest) -> None:
        """A hostile node id cannot turn the rejection into a huge payload."""
        huge_id = "h" * 50_000
        hub = _node(huge_id, NodeType.TRIGGER)
        targets = [_node(f"t{i}", NodeType.OUTPUT) for i in range(MAX_EDGES_PER_NODE + 1)]
        edges = [
            _edge(f"e{i}", huge_id, f"t{i}")
            for i in range(MAX_EDGES_PER_NODE + 1)
        ]
        result = WorkflowValidator.validate(_definition([hub, *targets], edges), manifest)
        message = next(
            e.message for e in result.errors if e.code == "NODE_FAN_OUT_TOO_HIGH"
        )
        assert len(message) <= 300


# ---------------------------------------------------------------------------
# Shape 3 — config size (per node and total)
# ---------------------------------------------------------------------------


class TestConfigSizeBound:
    def test_oversized_single_node_config_refused(self, manifest: CapabilityManifest) -> None:
        fat = {"payload": "x" * (MAX_CONFIG_BYTES_PER_NODE + 1024)}
        nodes = [
            _node("trigger", NodeType.TRIGGER),
            _node("fat", NodeType.TRANSFORM, config=fat),
            _node("out", NodeType.OUTPUT),
        ]
        started = time.perf_counter()
        result = WorkflowValidator.validate(_definition(nodes, []), manifest)
        elapsed = time.perf_counter() - started
        assert "NODE_CONFIG_TOO_LARGE" in _codes(result)
        assert elapsed < REFUSAL_TIME_LIMIT_S

    def test_multi_mb_config_refused(self, manifest: CapabilityManifest) -> None:
        """Many medium nodes summing past the total cap refuse — the shape a
        per-field length cap cannot see.  Isolated: node count stays at-limit
        and each node stays under the per-node cap, so ONLY the graph-total
        cap fires."""
        per_node = {"payload": "x" * 7_168}  # ~7 KB each: legal per field and per node
        nodes = [_node(f"n{i}", NodeType.TRANSFORM, config=per_node) for i in range(199)]
        nodes.append(_node("out", NodeType.OUTPUT))
        assert len(nodes) <= MAX_GRAPH_NODES
        assert 199 * 7_168 > MAX_CONFIG_BYTES_TOTAL  # ~1.4 MB total > 1 MB cap
        started = time.perf_counter()
        result = WorkflowValidator.validate(_definition(nodes, []), manifest)
        elapsed = time.perf_counter() - started
        assert result.valid is False
        assert _codes(result) == {"GRAPH_CONFIG_TOO_LARGE"}
        assert elapsed < REFUSAL_TIME_LIMIT_S

    def test_config_size_boundary_accepted(self, manifest: CapabilityManifest) -> None:
        """A node exactly at the per-node cap is not a resource error."""
        # JSON of ``{"a": "<n>"}`` adds ~8 bytes of syntax around the string.
        filler = "x" * (MAX_CONFIG_BYTES_PER_NODE - len('{"a": ""}'))
        nodes = [
            _node("trigger", NodeType.TRIGGER),
            _node("fat", NodeType.TRANSFORM, config={"a": filler}),
            _node("out", NodeType.OUTPUT),
        ]
        result = WorkflowValidator.validate(_definition(nodes, []), manifest)
        assert "NODE_CONFIG_TOO_LARGE" not in _codes(result)
        assert "GRAPH_CONFIG_TOO_LARGE" not in _codes(result)

    def test_config_size_one_over_refused(self, manifest: CapabilityManifest) -> None:
        filler = "x" * (MAX_CONFIG_BYTES_PER_NODE - len('{"a": ""}') + 1)
        nodes = [
            _node("trigger", NodeType.TRIGGER),
            _node("fat", NodeType.TRANSFORM, config={"a": filler}),
            _node("out", NodeType.OUTPUT),
        ]
        result = WorkflowValidator.validate(_definition(nodes, []), manifest)
        assert "NODE_CONFIG_TOO_LARGE" in _codes(result)


# ---------------------------------------------------------------------------
# The caps sit at the ONE enforcement point — validate() and via it compile()
# ---------------------------------------------------------------------------


class TestSingleEnforcementPoint:
    def test_compile_revalidates_and_refuses_oversized(self, manifest: CapabilityManifest) -> None:
        """WorkflowCompiler re-validates: an oversized definition cannot be
        compiled by constructing a revision around the validator."""
        nodes = [_node(f"n{i}", NodeType.TRIGGER) for i in range(MAX_GRAPH_NODES + 1)]
        nodes.append(_node("out", NodeType.OUTPUT))
        revision = WorkflowRevision.create(_definition(nodes, []), revision=1)
        with pytest.raises(ValueError) as exc_info:
            WorkflowCompiler.compile(revision, manifest=manifest)
        assert "TOO_MANY_NODES" in str(exc_info.value)

    def test_compile_refuses_fan_out_hub(self, manifest: CapabilityManifest) -> None:
        hub = _node("hub", NodeType.TRIGGER)
        targets = [_node(f"t{i}", NodeType.OUTPUT) for i in range(MAX_EDGES_PER_NODE + 1)]
        edges = [
            _edge(f"e{i}", "hub", f"t{i}")
            for i in range(MAX_EDGES_PER_NODE + 1)
        ]
        revision = WorkflowRevision.create(_definition([hub, *targets], edges), revision=1)
        with pytest.raises(ValueError) as exc_info:
            WorkflowCompiler.compile(revision, manifest=manifest)
        assert "NODE_FAN_OUT_TOO_HIGH" in str(exc_info.value)

    def test_compile_refuses_oversized_config(self, manifest: CapabilityManifest) -> None:
        fat = {"payload": "x" * (MAX_CONFIG_BYTES_PER_NODE + 1024)}
        nodes = [
            _node("trigger", NodeType.TRIGGER),
            _node("fat", NodeType.TRANSFORM, config=fat),
            _node("out", NodeType.OUTPUT),
        ]
        revision = WorkflowRevision.create(_definition(nodes, []), revision=1)
        with pytest.raises(ValueError) as exc_info:
            WorkflowCompiler.compile(revision, manifest=manifest)
        assert "NODE_CONFIG_TOO_LARGE" in str(exc_info.value)

    def test_small_valid_definition_still_compiles(self, manifest: CapabilityManifest) -> None:
        """No over-rejection: the pre-pass is transparent to legal definitions."""
        nodes = [
            _node("trigger", NodeType.TRIGGER),
            _node("transform", NodeType.TRANSFORM, config={"template": "hi"}),
            _node("out", NodeType.OUTPUT),
        ]
        edges = [_edge("e1", "trigger", "transform"), _edge("e2", "transform", "out")]
        result = WorkflowValidator.validate(_definition(nodes, edges), manifest)
        assert result.valid, result.error_summary
        revision = WorkflowRevision.create(_definition(nodes, edges), revision=1)
        plan = WorkflowCompiler.compile(revision, manifest=manifest)
        assert plan.revision_hash == revision.content_hash


# ---------------------------------------------------------------------------
# Layered policy: import-time caps and validator caps agree
# ---------------------------------------------------------------------------


class TestLayerParity:
    def test_validator_caps_match_import_time_caps(self) -> None:
        """One policy across layers: the adapter's import-time node/edge caps
        equal the validator's (a definition legal to import is legal to
        validate on those dimensions)."""
        from workflow_engine.adapters.langflow_adapter import LangflowAdapter

        assert LangflowAdapter.MAX_NODES == MAX_GRAPH_NODES
        assert LangflowAdapter.MAX_EDGES == MAX_GRAPH_EDGES


# ---------------------------------------------------------------------------
# Import layer refuses fast too (existing adapter caps, proven here)
# ---------------------------------------------------------------------------


class TestImportLayerRefusesFast:
    def test_10k_node_flow_refused_at_import(self) -> None:
        """The untrusted-JSON boundary refuses before any translation work:
        the adapter's import-time node cap fires on the raw node count."""
        import json

        from workflow_engine import LangflowAdapter, LangflowImportError

        raw_nodes = [
            {"id": f"n{i}", "type": "ChatInput", "data": {"type": "ChatInput"}}
            for i in range(10_000)
        ]
        flow = {"id": "f", "name": "huge", "data": {"nodes": raw_nodes, "edges": []}}
        body = json.dumps(flow)
        started = time.perf_counter()
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_definition(
                json.loads(body), workflow_id="wf-1", workspace_id="ws-1"
            )
        elapsed = time.perf_counter() - started
        assert "OVERSIZED_FLOW" in {r["code"] for r in exc_info.value.rejections}
        assert elapsed < REFUSAL_TIME_LIMIT_S
