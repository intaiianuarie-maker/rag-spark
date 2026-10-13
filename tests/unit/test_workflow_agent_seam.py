"""T-V4-011 — AGENT node execution through the abstract adapter seam.

Routing rule (stated): AGENT nodes execute through the INJECTED seam
(``configure_agent_executor`` — production wires the W3 ``V3NodeExecutor``
from ``workflow_engine.adapters.v3_adapter``); every other node, and AGENT
nodes with no seam wired, execute through the primary injected executor.
The executor imports neither the adapter nor the V3 product — seam-only,
mirroring the T-V4-015 rule (adapters import the executor, never the
reverse, so there is no cycle and no direct v3 import).

All three card metrics are proven: AGENT nodes bypassing the seam: 0
(every AGENT execution reaches the injected runner exactly once,
spy-verified); direct V3 imports in executor: 0 (source grep + subprocess
import-graph check); flag-off behavior change: 0 (default wiring keeps the
fake path byte-identical, plus a subprocess parity run).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from workflow_engine import (
    CapabilityManifest,
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
from workflow_engine.adapters.v3_adapter import (
    ADAPTER_FLAG_ENV,
    V3AdapterDisabled,
    V3NodeExecutor,
    resolve_node_executor,
)


class _DelegationSpy:
    """Injected AgentRunner: records every delegation request verbatim."""

    def __init__(self, outputs: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outputs = (
            outputs if outputs is not None else {"output": {"deep": [1, 2]}}
        )

    async def __call__(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(request))
        return dict(self._outputs)


class _FakePrimary:
    """Primary executor for non-AGENT nodes; records what it was asked."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute_node(
        self,
        node: NodeSpec,
        input_data: dict[str, Any] | Any,
        workspace_id: str,
    ) -> dict[str, Any]:
        self.calls.append(node.id)
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
    capabilities: tuple[str, ...] = (),
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        type=node_type,
        config=config or {},
        input_ports=[PortSpec(name=p, type="any") for p in in_ports],
        output_ports=[PortSpec(name=p, type="any") for p in out_ports],
        required_capabilities=list(capabilities),
    )


def _edge(edge_id: str, source: str, target: str, target_port: str = "input") -> WorkflowEdgeSpec:
    return WorkflowEdgeSpec(
        id=edge_id,
        source_node_id=source,
        source_port="output",
        target_node_id=target,
        target_port=target_port,
    )


#: Compile-time manifest: permissive on the `agent` capability so the
#: capability REFUSAL under test happens at the adapter seam (runtime), not
#: at compile time.
_MANIFEST = CapabilityManifest(
    supported_node_types=set(NodeType), enabled_capabilities={"agent"}
)


def _agent_flow(*, agent_id: str = "agent", capabilities: tuple[str, ...] = ()) -> tuple:
    """trigger → AGENT → output."""
    nodes = [
        _node("start", NodeType.TRIGGER, in_ports=()),
        _node(agent_id, NodeType.AGENT, out_ports=("out", "output"), capabilities=capabilities),
        _node("end", NodeType.OUTPUT, in_ports=("input",)),
    ]
    edges = [
        _edge("e1", "start", agent_id),
        _edge("e2", agent_id, "end"),
    ]
    spec = WorkflowDefinition(
        id="wf-agent", workspace_id="ws-1", name="Agent",
        nodes=nodes, edges=edges,
    )
    revision = WorkflowRevision.create(spec, revision=1)
    return WorkflowCompiler.compile(revision, manifest=_MANIFEST), revision


async def _run(plan, revision, *, agent_executor=None, initial=None) -> tuple:
    primary = _FakePrimary()
    executor = WorkflowExecutor(primary)
    if agent_executor is not None:
        executor.configure_agent_executor(agent_executor)
    run = await executor.execute(plan, revision, initial or {"task": "do it"})
    return run, primary, executor


# ---------------------------------------------------------------------------
# Metric 1 — AGENT nodes bypassing the seam: 0
# ---------------------------------------------------------------------------


class TestAgentNodesRouteThroughTheSeam:
    async def test_agent_execution_reaches_runner_exactly_once(self) -> None:
        spy = _DelegationSpy()
        adapter = V3NodeExecutor(agent_runner=spy, enabled=True)
        plan, revision = _agent_flow()
        run, primary, _ = await _run(plan, revision, agent_executor=adapter)

        assert run.status == WorkflowRunStatus.COMPLETED
        # Exactly one delegation, carrying the full request.
        assert len(spy.calls) == 1
        request = spy.calls[0]
        assert request["node_id"] == "agent"
        assert request["node_type"] == str(NodeType.AGENT)
        assert request["workspace_id"] == "ws-1"
        assert request["input_data"] == {"task": "do it"}
        assert adapter.dispatches == 1
        # The authority's output is recorded UNCHANGED.
        assert run.node_runs["agent"].output_data == {"output": {"deep": [1, 2]}}
        assert run.node_runs["agent"].status == NodeRunStatus.COMPLETED
        # Non-AGENT nodes did not reach the runner.
        assert primary.calls == ["start", "end"]

    async def test_each_agent_node_delegates_once(self) -> None:
        spy = _DelegationSpy()
        adapter = V3NodeExecutor(agent_runner=spy, enabled=True)
        nodes = [
            _node("start", NodeType.TRIGGER, in_ports=()),
            _node("a1", NodeType.AGENT, out_ports=("out", "output")),
            _node("a2", NodeType.AGENT, out_ports=("out", "output")),
            _node("end", NodeType.OUTPUT, in_ports=("i1", "i2")),
        ]
        edges = [
            _edge("e1", "start", "a1"),
            _edge("e2", "start", "a2"),
            _edge("e3", "a1", "end", target_port="i1"),
            _edge("e4", "a2", "end", target_port="i2"),
        ]
        # end merges two branches: declare the barrier (T-V4-008).
        nodes[3] = nodes[3].model_copy(
            update={"join_strategy": JoinStrategy.ALL}
        )
        spec = WorkflowDefinition(
            id="wf-agent2", workspace_id="ws-1", name="Agents",
            nodes=nodes, edges=edges,
        )
        revision = WorkflowRevision.create(spec, revision=1)
        plan = WorkflowCompiler.compile(revision, manifest=_MANIFEST)
        run, primary, _ = await _run(plan, revision, agent_executor=adapter)
        assert run.status == WorkflowRunStatus.COMPLETED
        assert sorted(c["node_id"] for c in spy.calls) == ["a1", "a2"]
        assert adapter.dispatches == 2
        assert primary.calls == ["start", "end"]

    async def test_non_agent_nodes_never_reach_the_seam(self) -> None:
        spy = _DelegationSpy()
        adapter = V3NodeExecutor(agent_runner=spy, enabled=True)
        plan, revision = _agent_flow()
        run, _, _ = await _run(plan, revision, agent_executor=adapter)
        # Only the AGENT node delegated.
        assert [c["node_id"] for c in spy.calls] == ["agent"]
        assert run.status == WorkflowRunStatus.COMPLETED

    async def test_wiring_type_is_validated(self) -> None:
        executor = WorkflowExecutor(_FakePrimary())
        with pytest.raises(TypeError):
            executor.configure_agent_executor("not-an-executor")  # type: ignore[arg-type]
        executor.configure_agent_executor(None)  # restoring default is legal
        assert executor.agent_executor is None


# ---------------------------------------------------------------------------
# Metric 2 — direct V3 imports in executor: 0
# ---------------------------------------------------------------------------


class TestNoDirectV3Imports:
    def test_executor_source_has_no_v3_imports(self) -> None:
        source = (
            Path(__file__).resolve().parents[2]
            / "packages" / "workflow_engine" / "executor.py"
        ).read_text()
        for banned in ("import agents", "from agents", "packages.agents", "packages/agents"):
            assert banned not in source, f"executor.py references V3 product code: {banned}"

    def test_import_graph_contains_no_v3_modules(self, tmp_path: Path) -> None:
        """Subprocess proof: importing the executor pulls in no `agents` module."""
        script = tmp_path / "import_check.py"
        script.write_text(
            "import sys\n"
            "import workflow_engine.executor  # noqa: F401\n"
            "leaked = [m for m in sys.modules if m == 'agents' or m.startswith('agents.')]\n"
            "print(leaked)\n"
        )
        repo_root = Path(__file__).resolve().parents[2]
        out = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            env={
                "PYTHONPATH": str(repo_root / "packages"),
                "PATH": "/usr/bin:/bin",
            },
            check=True,
            timeout=60,
        )
        assert out.stdout.strip() == "[]", f"V3 modules imported: {out.stdout}"


# ---------------------------------------------------------------------------
# Metric 3 — flag-off behavior change: 0
# ---------------------------------------------------------------------------


class TestFlagOffUnchanged:
    async def test_default_wiring_keeps_the_fake_path(self) -> None:
        """No seam wired (the default): the AGENT node goes to the injected
        executor exactly as before T-V4-011 — the W0/W1 fake raises
        NotImplementedError, the run FAILS with the same message, and no
        runner is contacted."""
        from workflow_engine.executor import FakeNodeExecutor

        plan, revision = _agent_flow()
        executor = WorkflowExecutor(FakeNodeExecutor())
        run = await executor.execute(plan, revision, {"task": "do it"})
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id == "agent"
        assert run.node_runs["agent"].status == NodeRunStatus.FAILED
        assert "not implemented" in (run.node_runs["agent"].error or "").lower()
        assert executor.agent_executor is None

    def test_resolve_seam_flag_off_returns_default(self) -> None:
        """The production seam function with the flag off returns the default
        executor (the adapter is never constructed)."""
        executor = resolve_node_executor(env={})
        from workflow_engine import FakeNodeExecutor

        assert isinstance(executor, FakeNodeExecutor)
        assert not isinstance(executor, V3NodeExecutor)

    def test_flag_off_parity_run(self, tmp_path: Path) -> None:
        """Subprocess parity: flag-off execution produces the documented
        statuses and error text (no behavioral drift)."""
        script = tmp_path / "flagoff_run.py"
        script.write_text(
            "import asyncio\n"
            "from workflow_engine import (\n"
            "    NodeSpec, NodeType, PortSpec, WorkflowCompiler,\n"
            "    WorkflowDefinition, WorkflowEdgeSpec, WorkflowExecutor,\n"
            "    WorkflowRevision,\n"
            ")\n"
            "from workflow_engine.executor import FakeNodeExecutor\n"
            "n = lambda i, t, ip=('input',), op=('output',): NodeSpec(\n"
            "    id=i, type=t,\n"
            "    input_ports=[PortSpec(name=p, type='any') for p in ip],\n"
            "    output_ports=[PortSpec(name=p, type='any') for p in op])\n"
            "e = lambda eid, s, t: WorkflowEdgeSpec(\n"
            "    id=eid, source_node_id=s, source_port='output',\n"
            "    target_node_id=t, target_port='input')\n"
            "spec = WorkflowDefinition(\n"
            "    id='wf', workspace_id='ws', name='off',\n"
            "    nodes=[n('start', NodeType.TRIGGER, ip=()),\n"
            "            n('agent', NodeType.AGENT),\n"
            "            n('end', NodeType.OUTPUT)],\n"
            "    edges=[e('e1', 'start', 'agent'), e('e2', 'agent', 'end')])\n"
            "rev = WorkflowRevision.create(spec, revision=1)\n"
            "plan = WorkflowCompiler.compile(rev)\n"
            "run = asyncio.run(WorkflowExecutor(FakeNodeExecutor()).execute(\n"
            "    plan, rev, {'task': 'x'}))\n"
            "print(run.status.value, run.node_runs['agent'].status.value,\n"
            "      'not implemented' in (run.node_runs['agent'].error or '').lower())\n"
        )
        repo_root = Path(__file__).resolve().parents[2]
        out = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            env={
                "PYTHONPATH": str(repo_root / "packages"),
                "PATH": "/usr/bin:/bin",
            },
            check=True,
            timeout=60,
        )
        assert out.stdout.strip() == "failed failed True"


# ---------------------------------------------------------------------------
# The seam's refusals are honoured (never swallowed)
# ---------------------------------------------------------------------------


class TestSeamRefusalsHonoured:
    async def test_disabled_adapter_refusal_surfaces(self) -> None:
        """A disabled adapter refuses explicitly: the node fails, the run
        fails naming it, and the runner is never touched."""
        spy = _DelegationSpy()
        adapter = V3NodeExecutor(agent_runner=spy)  # enabled=False
        plan, revision = _agent_flow()
        run, _, _ = await _run(
            plan, revision,
            agent_executor=adapter,
        )
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id == "agent"
        assert "disabled" in (run.error or "").lower()
        assert spy.calls == []
        assert adapter.dispatches == 0

    async def test_missing_capability_refusal_surfaces(self) -> None:
        """A capability outside the manifest fails the node — no mock success."""
        spy = _DelegationSpy()
        adapter = V3NodeExecutor(
            agent_runner=spy,
            manifest=CapabilityManifest(enabled_capabilities=set()),
            enabled=True,
        )
        plan, revision = _agent_flow(capabilities=("agent",))
        run, _, _ = await _run(plan, revision, agent_executor=adapter)
        assert run.status == WorkflowRunStatus.FAILED
        assert run.failed_node_id == "agent"
        assert run.node_runs["agent"].status == NodeRunStatus.FAILED
        assert "unavailable capabilities" in (run.node_runs["agent"].error or "")
        assert spy.calls == []  # the authority is never reached

    async def test_missing_capability_is_permanent_no_retry(self) -> None:
        spy = _DelegationSpy()
        adapter = V3NodeExecutor(
            agent_runner=spy,
            manifest=CapabilityManifest(enabled_capabilities=set()),
            enabled=True,
        )
        plan, revision = _agent_flow(capabilities=("agent",))
        run, _, _ = await _run(plan, revision, agent_executor=adapter)
        # NotImplementedError is permanent (T-V4-010): exactly one attempt.
        assert run.node_runs["agent"].attempts == 1

    def test_no_runner_with_flag_on_is_a_wiring_error(self) -> None:
        """Enabling without a runner refuses — never a silent default."""
        with pytest.raises(V3AdapterDisabled):
            resolve_node_executor(agent_runner=None, env={ADAPTER_FLAG_ENV: "1"})

    async def test_flag_on_with_runner_delegates(self) -> None:
        spy = _DelegationSpy()
        adapter = resolve_node_executor(
            agent_runner=spy, env={ADAPTER_FLAG_ENV: "1"}
        )
        plan, revision = _agent_flow()
        run, _, _ = await _run(plan, revision, agent_executor=adapter)
        assert run.status == WorkflowRunStatus.COMPLETED
        assert len(spy.calls) == 1
        assert isinstance(adapter, V3NodeExecutor)
        assert adapter.enabled is True
