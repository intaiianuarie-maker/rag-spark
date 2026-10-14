"""Unit tests for the W0/W1 fake runtime executor.

Tests linear execution, conditional branching (both outcomes), skipped
branches, skipped joins, nested branches, and failure honesty (unsupported
node types raise NotImplementedError, never mock success).  No real
LLM/model/tool calls; no LangGraph/Langflow integration.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Any

import pytest
from workflow_engine import (
    CapabilityManifest,
    ExecutionPlan,
    FakeNodeExecutor,
    JoinStrategy,
    NodeExecutor,
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _compile(defn: WorkflowDefinition) -> ExecutionPlan:
    rev = _make_revision(defn)
    return WorkflowCompiler.compile(rev, allow_unvalidated=True)


async def _run(defn: WorkflowDefinition, initial_input: dict) -> tuple:
    rev = _make_revision(defn)
    plan = WorkflowCompiler.compile(rev, allow_unvalidated=True)
    executor = FakeNodeExecutor()
    runner = WorkflowExecutor(executor)
    run = await runner.execute(plan, rev, initial_input)
    return run, rev, plan


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

def test_fake_executor_implements_protocol() -> None:
    """FakeNodeExecutor must be usable as a NodeExecutor (runtime_checkable)."""
    assert isinstance(FakeNodeExecutor(), NodeExecutor)


# ---------------------------------------------------------------------------
# Linear execution
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_linear_workflow_execution() -> None:
    defn = WorkflowDefinition(
        id="wf-linear", workspace_id="ws", name="Linear",
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="output", type="dict")]),
            NodeSpec(id="fmt", type=NodeType.TRANSFORM, name="Format",
                     config={"template": "Hi {name}!"},
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="string")]),
            NodeSpec(id="end", type=NodeType.OUTPUT, name="End",
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                             target_node_id="fmt", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="fmt", source_port="output",
                             target_node_id="end", target_port="input"),
        ],
    )
    run, _, _ = await _run(defn, {"name": "Alice"})
    assert run.status == WorkflowRunStatus.COMPLETED
    assert run.final_output == "Hi Alice!"
    assert run.node_runs["start"].status == NodeRunStatus.COMPLETED
    assert run.node_runs["fmt"].status == NodeRunStatus.COMPLETED
    assert run.node_runs["end"].status == NodeRunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Conditional branching — both outcomes
# ---------------------------------------------------------------------------

def _branch_defn() -> WorkflowDefinition:
    return WorkflowDefinition(
        id="wf-branch", workspace_id="ws", name="Branch",
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="output", type="dict")]),
                        NodeSpec(id="cond", type=NodeType.CONDITION, name="Check",
                     config={"field": "doc_type", "expected": "legal"},
                     input_ports=[PortSpec(name="input", type="dict")],
                     output_ports=[
                         PortSpec(name="result", type="boolean"),
                         PortSpec(name="output", type="dict"),
                     ]),
            NodeSpec(id="legal_out", type=NodeType.OUTPUT, name="Legal",
                     input_ports=[PortSpec(name="input", type="any")]),
            NodeSpec(id="general_out", type=NodeType.OUTPUT, name="General",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                             target_node_id="cond", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="cond", source_port="output",
                             target_node_id="legal_out", target_port="input",
                             condition_value=True),
            WorkflowEdgeSpec(id="e3", source_node_id="cond", source_port="output",
                             target_node_id="general_out", target_port="input",
                             condition_value=False),
        ],
    )


@pytest.mark.unit
async def test_conditional_branch_legal() -> None:
    defn = _branch_defn()
    run, _, _ = await _run(defn, {"doc_type": "legal", "text": "Contract"})
    assert run.status == WorkflowRunStatus.COMPLETED
    assert run.node_runs["legal_out"].status == NodeRunStatus.COMPLETED
    assert run.node_runs["general_out"].status == NodeRunStatus.SKIPPED


@pytest.mark.unit
async def test_conditional_branch_general() -> None:
    defn = _branch_defn()
    run, _, _ = await _run(defn, {"doc_type": "marketing", "text": "Blog"})
    assert run.status == WorkflowRunStatus.COMPLETED
    assert run.node_runs["legal_out"].status == NodeRunStatus.SKIPPED
    assert run.node_runs["general_out"].status == NodeRunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Skipped branch propagation (skipped join)
# ---------------------------------------------------------------------------

def _skipped_join_defn() -> WorkflowDefinition:
    """Condition fan-out → two transform branches → merge (all-join) → output.

    When the condition skips one branch, the all-join merge node also gets
    skipped (it requires both branches).
    """
    return WorkflowDefinition(
        id="wf-skip-join", workspace_id="ws", name="SkipJoin",
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="output", type="dict")]),
            NodeSpec(id="cond", type=NodeType.CONDITION, name="Check",
                     config={"field": "mode", "expected": "fast"},
                     input_ports=[PortSpec(name="input", type="dict")],
                     output_ports=[PortSpec(name="result", type="boolean")]),
            NodeSpec(id="fast_branch", type=NodeType.TRANSFORM, name="Fast",
                     config={"template": "fast:{input}"},
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="slow_branch", type=NodeType.TRANSFORM, name="Slow",
                     config={"template": "slow:{input}"},
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="merge", type=NodeType.TRANSFORM, name="Merge",
                     config={"key": "output"},
                     input_ports=[
                         PortSpec(name="fast_branch", type="any"),
                         PortSpec(name="slow_branch", type="any"),
                     ],
                     output_ports=[PortSpec(name="output", type="any")],
                     join_strategy=JoinStrategy.ALL),
            NodeSpec(id="end", type=NodeType.OUTPUT, name="End",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                             target_node_id="cond", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="cond", source_port="result",
                             target_node_id="fast_branch", target_port="input",
                             condition_value=True),
            WorkflowEdgeSpec(id="e3", source_node_id="cond", source_port="result",
                             target_node_id="slow_branch", target_port="input",
                             condition_value=False),
            WorkflowEdgeSpec(id="e4", source_node_id="fast_branch", source_port="output",
                             target_node_id="merge", target_port="fast_branch"),
            WorkflowEdgeSpec(id="e5", source_node_id="slow_branch", source_port="output",
                             target_node_id="merge", target_port="slow_branch"),
            WorkflowEdgeSpec(id="e6", source_node_id="merge", source_port="output",
                             target_node_id="end", target_port="input"),
        ],
    )


@pytest.mark.unit
async def test_skipped_branch_skips_join() -> None:
    """T-V4-009: an all-barrier join left incomplete is NOT a silent success.

    One branch is skipped by the condition, so the declared all-barrier
    merge receives partial inputs.  The merge is skipped with the reason
    recorded, the run FAILS naming the merge node, and nothing reports
    COMPLETED while the declared work is incomplete.
    """
    defn = _skipped_join_defn()
    run, _, _ = await _run(defn, {"mode": "fast"})
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failed_node_id == "merge"
    assert "barrier-incomplete" in (run.error or "")
    assert run.node_runs["cond"].status == NodeRunStatus.COMPLETED
    assert run.node_runs["fast_branch"].status == NodeRunStatus.COMPLETED
    assert run.node_runs["slow_branch"].status == NodeRunStatus.SKIPPED
    # Merge should be skipped since it requires both branches (all-join) —
    # and the skip is disclosed with its reason, not silent.
    assert run.node_runs["merge"].status == NodeRunStatus.SKIPPED
    assert "barrier-incomplete" in (run.node_runs["merge"].error or "")
    # end is single-inbound downstream of the skipped merge: routing skip.
    assert run.node_runs["end"].status == NodeRunStatus.SKIPPED
    assert "no-active-inbound-edge" in (run.node_runs["end"].error or "")


@pytest.mark.unit
async def test_skipped_branch_both_branches_run_when_no_condition() -> None:
    """Without conditional edges, both branches run and merge succeeds."""
    defn = _skipped_join_defn()
    # When condition_values are removed, edges become unconditional.
    # Both branches should run, and the merge should execute.
    for edge in defn.edges:
        if edge.source_node_id == "cond":
            edge.condition_value = None
    # Fix merge node to pass through input (remove key-extraction config).
    for node in defn.nodes:
        if node.id == "merge":
            node.config = {}

    run, _, _ = await _run(defn, {"mode": "fast"})
    assert run.status == WorkflowRunStatus.COMPLETED
    assert run.node_runs["fast_branch"].status == NodeRunStatus.COMPLETED
    assert run.node_runs["slow_branch"].status == NodeRunStatus.COMPLETED
    assert run.node_runs["merge"].status == NodeRunStatus.COMPLETED
    assert run.node_runs["end"].status == NodeRunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Nested branch
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_nested_branch() -> None:
    """Condition within a condition branch — both levels evaluate correctly."""
    defn = WorkflowDefinition(
        id="wf-nested", workspace_id="ws", name="Nested",
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="output", type="dict")]),
            NodeSpec(id="outer_cond", type=NodeType.CONDITION, name="Outer",
                     config={"field": "region", "expected": "us"},
                     input_ports=[PortSpec(name="input", type="dict")],
                     output_ports=[
                         PortSpec(name="result", type="boolean"),
                         PortSpec(name="output", type="dict"),
                     ]),
            NodeSpec(id="inner_cond", type=NodeType.CONDITION, name="Inner",
                     config={"field": "doc_type", "expected": "legal"},
                     input_ports=[PortSpec(name="input", type="dict")],
                     output_ports=[
                         PortSpec(name="result", type="boolean"),
                         PortSpec(name="output", type="dict"),
                     ]),
            NodeSpec(id="us_legal_out", type=NodeType.OUTPUT, name="US Legal",
                     input_ports=[PortSpec(name="input", type="any")]),
            NodeSpec(id="us_other_out", type=NodeType.OUTPUT, name="US Other",
                     input_ports=[PortSpec(name="input", type="any")]),
            NodeSpec(id="non_us_out", type=NodeType.OUTPUT, name="Non-US",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                             target_node_id="outer_cond", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="outer_cond", source_port="output",
                             target_node_id="inner_cond", target_port="input",
                             condition_value=True),
            WorkflowEdgeSpec(id="e3", source_node_id="outer_cond", source_port="output",
                             target_node_id="non_us_out", target_port="input",
                             condition_value=False),
            WorkflowEdgeSpec(id="e4", source_node_id="inner_cond", source_port="output",
                             target_node_id="us_legal_out", target_port="input",
                             condition_value=True),
            WorkflowEdgeSpec(id="e5", source_node_id="inner_cond", source_port="output",
                             target_node_id="us_other_out", target_port="input",
                             condition_value=False),
        ],
    )

    # Test: us + legal → inner_cond runs, us_legal_out completes, others skipped
    run, _, _ = await _run(defn, {"region": "us", "doc_type": "legal", "text": "Contract"})
    assert run.status == WorkflowRunStatus.COMPLETED
    assert run.node_runs["outer_cond"].status == NodeRunStatus.COMPLETED
    assert run.node_runs["inner_cond"].status == NodeRunStatus.COMPLETED
    assert run.node_runs["us_legal_out"].status == NodeRunStatus.COMPLETED
    assert run.node_runs["us_other_out"].status == NodeRunStatus.SKIPPED
    assert run.node_runs["non_us_out"].status == NodeRunStatus.SKIPPED

    # Test: us + non-legal → inner_cond runs, us_other_out completes, others skipped
    run2, _, _ = await _run(defn, {"region": "us", "doc_type": "blog", "text": "Post"})
    assert run2.status == WorkflowRunStatus.COMPLETED
    assert run2.node_runs["inner_cond"].status == NodeRunStatus.COMPLETED
    assert run2.node_runs["us_legal_out"].status == NodeRunStatus.SKIPPED
    assert run2.node_runs["us_other_out"].status == NodeRunStatus.COMPLETED
    assert run2.node_runs["non_us_out"].status == NodeRunStatus.SKIPPED

    # Test: non-us → inner_cond skipped, non_us_out completes
    run3, _, _ = await _run(defn, {"region": "eu", "doc_type": "legal", "text": "Contract"})
    assert run3.status == WorkflowRunStatus.COMPLETED
    assert run3.node_runs["inner_cond"].status == NodeRunStatus.SKIPPED
    assert run3.node_runs["non_us_out"].status == NodeRunStatus.COMPLETED
    assert run3.node_runs["us_legal_out"].status == NodeRunStatus.SKIPPED
    assert run3.node_runs["us_other_out"].status == NodeRunStatus.SKIPPED


# ---------------------------------------------------------------------------
# Failure honesty — unsupported node types
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_unsupported_node_type_raises_not_implemented() -> None:
    """Agent node type not implemented in fake runtime → NotImplementedError."""
    defn = WorkflowDefinition(
        id="wf-unsupported", workspace_id="ws", name="Unsupported",
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="agent", type=NodeType.AGENT, name="Agent",
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="end", type=NodeType.OUTPUT, name="End",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                             target_node_id="agent", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="agent", source_port="output",
                             target_node_id="end", target_port="input"),
        ],
    )
    run, _, _ = await _run(defn, {"text": "hello"})
    assert run.status == WorkflowRunStatus.FAILED
    assert run.node_runs["agent"].status == NodeRunStatus.FAILED
    assert "not implemented" in run.node_runs["agent"].error.lower()


@pytest.mark.unit
async def test_unsupported_capability_raises_not_implemented() -> None:
    """A node with required_capabilities not available in fake → NotImplementedError."""
    defn = WorkflowDefinition(
        id="wf-cap", workspace_id="ws", name="Cap",
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="output", type="any")]),
            NodeSpec(id="llm_node", type=NodeType.LLM, name="LLM",
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")],
                     required_capabilities=["llm"]),
            NodeSpec(id="end", type=NodeType.OUTPUT, name="End",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                             target_node_id="llm_node", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="llm_node", source_port="output",
                             target_node_id="end", target_port="input"),
        ],
    )
    run, _, _ = await _run(defn, {"text": "hello"})
    assert run.status == WorkflowRunStatus.FAILED
    assert run.node_runs["llm_node"].status == NodeRunStatus.FAILED
    assert "capabilities" in run.node_runs["llm_node"].error.lower()


# ---------------------------------------------------------------------------
# Transform node with config
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_transform_template() -> None:
    defn = WorkflowDefinition(
        id="wf-tmpl", workspace_id="ws", name="Tmpl",
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="output", type="dict")]),
            NodeSpec(id="fmt", type=NodeType.TRANSFORM, name="Format",
                     config={"template": "Result: {value}"},
                     input_ports=[PortSpec(name="input", type="dict")],
                     output_ports=[PortSpec(name="output", type="string")]),
            NodeSpec(id="end", type=NodeType.OUTPUT, name="End",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                             target_node_id="fmt", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="fmt", source_port="output",
                             target_node_id="end", target_port="input"),
        ],
    )
    run, _, _ = await _run(defn, {"value": "test123"})
    assert run.status == WorkflowRunStatus.COMPLETED
    assert run.final_output == "Result: test123"


# ---------------------------------------------------------------------------
# Retriever node
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_retriever_node_execution() -> None:
    defn = WorkflowDefinition(
        id="wf-retriever", workspace_id="ws", name="Retriever",
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="output", type="dict")]),
            NodeSpec(id="retriever", type=NodeType.RETRIEVER, name="Search",
                     config={"chunk_ids": ["chunk-1", "chunk-2"]},
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="list")]),
            NodeSpec(id="end", type=NodeType.OUTPUT, name="End",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                             target_node_id="retriever", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="retriever", source_port="output",
                             target_node_id="end", target_port="input"),
        ],
    )
    run, _, _ = await _run(defn, {"query": "test"})
    assert run.status == WorkflowRunStatus.COMPLETED
    assert run.node_runs["retriever"].status == NodeRunStatus.COMPLETED
    assert run.final_output == ["chunk-1", "chunk-2"]


# ---------------------------------------------------------------------------
# Trigger node input handling
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_trigger_passes_initial_input() -> None:
    defn = WorkflowDefinition(
        id="wf-trigger", workspace_id="ws", name="Trigger",
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="output", type="dict")]),
            NodeSpec(id="end", type=NodeType.OUTPUT, name="End",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                             target_node_id="end", target_port="input"),
        ],
    )
    payload = {"message": "hello", "count": 42}
    run, _, _ = await _run(defn, payload)
    assert run.status == WorkflowRunStatus.COMPLETED
    # Trigger output should contain the initial_input.
    trigger_out = run.node_runs["start"].output_data
    assert trigger_out["output"] == payload
    # Final output should be the payload (passed through trigger→output).
    assert run.final_output == payload


# ---------------------------------------------------------------------------
# V3-backed NodeExecutor adapter skeleton (T-V4-015) — flagged off by default
#
# Appended by T-V4-015. These tests pin the adapter's three card rules: no
# direct v3 imports, flag-off never dispatches, missing capability → explicit
# failure — plus the flag-off parity property.
# ---------------------------------------------------------------------------

from workflow_engine.adapters.v3_adapter import (  # noqa: E402
    ADAPTER_FLAG_ENV,
    V3AdapterDisabled,
    V3NodeExecutor,
    adapter_enabled,
    resolve_node_executor,
)


class _DelegationSpy:
    """Stand-in for the V3 agents runtime: records every delegated request."""

    def __init__(self, outputs: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outputs = outputs if outputs is not None else {"out": "v3"}

    async def __call__(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(request))
        return dict(self._outputs)


def _agent_node(**caps: Any) -> NodeSpec:
    return NodeSpec(
        id="v3node",
        type=NodeType.AGENT,
        name="V3",
        input_ports=[PortSpec(name="input", type="any")],
        output_ports=[PortSpec(name="out", type="any")],
        required_capabilities=list(caps),
    )


def test_v3_adapter_implements_protocol() -> None:
    """The adapter must be usable as a NodeExecutor (runtime_checkable)."""
    assert isinstance(V3NodeExecutor(agent_runner=_DelegationSpy()), NodeExecutor)


def test_v3_adapter_flag_off_refuses_and_never_dispatches() -> None:
    """Card metric: flag-off dispatches: 0.

    A disabled adapter refuses explicitly (V3AdapterDisabled) and never touches
    the runner, so its dispatches counter stays at 0.
    """
    spy = _DelegationSpy()
    adapter = V3NodeExecutor(agent_runner=spy)  # enabled=False by default
    assert adapter.enabled is False
    node = _agent_node()

    with pytest.raises(V3AdapterDisabled):
        asyncio.run(adapter.execute_node(node, {"x": 1}, "ws-1"))

    assert adapter.dispatches == 0, "flag-off dispatches must be 0"
    assert spy.calls == [], "the V3 authority must never be reached while disabled"


def test_v3_adapter_delegates_every_call_to_the_v3_authority() -> None:
    """Card metric: every adapter call reaches the V3 authority.

    Each routed node arrives at the runner once, carrying the full delegation
    request (node id, type, config, workspace, inputs, capabilities).
    """
    spy = _DelegationSpy()
    adapter = V3NodeExecutor(agent_runner=spy, enabled=True)

    node = NodeSpec(
        id="v3node",
        type=NodeType.AGENT,
        name="V3",
        config={"model": "x"},
        input_ports=[PortSpec(name="input", type="any")],
        output_ports=[PortSpec(name="out", type="any")],
    )
    outputs = asyncio.run(adapter.execute_node(node, {"q": "hi"}, "ws-42"))

    assert adapter.dispatches == 1
    assert len(spy.calls) == 1
    req = spy.calls[0]
    assert req["node_id"] == "v3node"
    assert req["node_type"] == str(NodeType.AGENT)
    assert req["node_config"] == {"model": "x"}
    assert req["workspace_id"] == "ws-42"
    assert req["input_data"] == {"q": "hi"}
    assert req["required_capabilities"] == []
    # The adapter returns the authority's outputs, not a local fabrication.
    assert outputs == {"out": "v3"}


def test_v3_adapter_does_not_transform_authority_output() -> None:
    """Whatever the authority returns is returned unchanged (no mock success)."""
    authority_output = {"out": {"nested": [1, 2, 3]}, "extra": None}
    spy = _DelegationSpy(outputs=authority_output)
    adapter = V3NodeExecutor(agent_runner=spy, enabled=True)
    got = asyncio.run(adapter.execute_node(_agent_node(), {}, "ws-1"))
    assert got == authority_output


def test_v3_adapter_missing_capability_fails_explicitly() -> None:
    """Missing capability → explicit failure, never a silent downgrade."""
    spy = _DelegationSpy()
    manifest = CapabilityManifest(
        supported_node_types={NodeType.AGENT},
        enabled_capabilities={"llm"},
    )
    adapter = V3NodeExecutor(agent_runner=spy, manifest=manifest, enabled=True)

    with pytest.raises(NotImplementedError, match="unavailable capabilities"):
        asyncio.run(adapter.execute_node(_agent_node(retrieval=None), {}, "ws-1"))

    assert adapter.dispatches == 0, "a refused node must never be dispatched"


def test_v3_adapter_flag_reads_env_and_defaults_off() -> None:
    """The flag comes from the environment and defaults to off."""
    assert adapter_enabled({}) is False
    assert adapter_enabled({ADAPTER_FLAG_ENV: ""}) is False
    assert adapter_enabled({ADAPTER_FLAG_ENV: "0"}) is False
    assert adapter_enabled({ADAPTER_FLAG_ENV: "1"}) is True
    assert adapter_enabled({ADAPTER_FLAG_ENV: " true "}) is True
    assert adapter_enabled({ADAPTER_FLAG_ENV: "FALSE"}) is False


def test_resolve_node_executor_flag_off_returns_default_executor() -> None:
    """Seam: flag off → the existing default executor (no V3 in the path)."""
    resolved = resolve_node_executor(env={})
    from workflow_engine.production_executor import ProductionNodeExecutor

    assert isinstance(resolved, ProductionNodeExecutor)


def test_resolve_node_executor_flag_on_requires_a_runner() -> None:
    """Enabling without a runner is a configuration error, not a silent fallback."""
    with pytest.raises(V3AdapterDisabled, match="no agent_runner"):
        resolve_node_executor(env={ADAPTER_FLAG_ENV: "1"})


def test_resolve_node_executor_flag_on_wires_the_adapter() -> None:
    """Flag on → a V3NodeExecutor wired to the supplied runner."""
    spy = _DelegationSpy()
    resolved = resolve_node_executor(agent_runner=spy, env={ADAPTER_FLAG_ENV: "1"})
    assert isinstance(resolved, V3NodeExecutor)
    assert resolved.enabled is True
    assert resolved.dispatches == 0


def _norm_run(run: Any) -> dict[str, Any]:
    """Drop run identity + timing so two runs can be compared byte-for-byte."""
    data = run.model_dump(mode="json")
    for volatile in ("id", "created_at", "finished_at"):
        data.pop(volatile, None)
    for node_run in data.get("node_runs", {}).values():
        node_run.pop("latency_ms", None)
    return data


@pytest.mark.unit
async def test_flag_off_default_path_is_byte_identical() -> None:
    """Flag-off parity: resolving through the seam is identical to the default.

    With the flag off the seam yields the default executor, so a compiled
    workflow executes exactly as it does today — byte-identical modulo run
    identity and timing — and the V3 adapter is never consulted.
    """
    defn = WorkflowDefinition(
        id="wf-parity", workspace_id="ws", name="Parity",
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="output", type="dict")]),
            NodeSpec(id="fmt", type=NodeType.TRANSFORM, name="Format",
                     config={"template": "Hi {name}!"},
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="string")]),
            NodeSpec(id="end", type=NodeType.OUTPUT, name="End",
                     input_ports=[PortSpec(name="input", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                             target_node_id="fmt", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="fmt", source_port="output",
                             target_node_id="end", target_port="input"),
        ],
    )
    rev = _make_revision(defn)
    plan = WorkflowCompiler.compile(rev, allow_unvalidated=True)

    spy = _DelegationSpy()
    default_run = await WorkflowExecutor(FakeNodeExecutor()).execute(plan, rev, {"name": "Alice"})
    seam_run = await WorkflowExecutor(
        resolve_node_executor(agent_runner=spy, env={})
    ).execute(plan, rev, {"name": "Alice"})

    assert _norm_run(default_run) == _norm_run(seam_run), (
        "flag-off default path must be byte-identical to the current behaviour"
    )
    assert spy.calls == [], "the V3 authority must never be reached with the flag off"
    assert seam_run.status == WorkflowRunStatus.COMPLETED
