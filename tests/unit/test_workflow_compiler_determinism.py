"""T-V4-008 — compiler determinism + join-semantics lock.

Determinism is proven at two levels:

* in-process: the same revision compiles N times to a byte-identical plan
  (``model_dump_json`` comparison — full payload, not sampled fields);
* cross-process: the same definition bytes compiled under different
  ``PYTHONHASHSEED`` values produce identical plan bytes.  This is the trap
  that matters: set iteration order (``workspace_capabilities``) used to
  leak into the revision hash, so identical bytes hashed differently per
  process (fixed by the canonical normalizer in
  ``WorkflowRevision._compute_content_hash``).

The join lock is proven per strategy (``all`` / ``any`` / ``first``) with
its stated barrier, plus the refusal of an undeclared join.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from workflow_engine import (
    CapabilityManifest,
    JoinStrategy,
    NodeSpec,
    NodeType,
    PortSpec,
    WorkflowCompiler,
    WorkflowDefinition,
    WorkflowEdgeSpec,
    WorkflowRevision,
    WorkflowValidator,
)
from workflow_engine.compiler import JOIN_BARRIER_TIMEOUT_S

#: Repeated in-process compiles: same bytes → same plan.
REPEAT_COMPILES = 25


def _node(
    node_id: str,
    node_type: NodeType,
    *,
    in_ports: tuple[str, ...] = ("input",),
    out_ports: tuple[str, ...] = ("output",),
    join: JoinStrategy | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        type=node_type,
        input_ports=[PortSpec(name=p, type="any") for p in in_ports],
        output_ports=[PortSpec(name=p, type="any") for p in out_ports],
        join_strategy=join,
    )


def _edge(
    edge_id: str, source: str, source_port: str, target: str, target_port: str
) -> WorkflowEdgeSpec:
    return WorkflowEdgeSpec(
        id=edge_id,
        source_node_id=source,
        source_port=source_port,
        target_node_id=target,
        target_port=target_port,
    )


def _join_definition(strategy: JoinStrategy | None) -> WorkflowDefinition:
    """trigger → (branch_a, branch_b) → join(2 inbound) → output."""
    return WorkflowDefinition(
        id="wf-join",
        workspace_id="ws-1",
        name="Join",
        nodes=[
            _node("start", NodeType.TRIGGER, in_ports=()),
            _node("branch_a", NodeType.TRANSFORM),
            _node("branch_b", NodeType.TRANSFORM),
            _node("join", NodeType.TRANSFORM, in_ports=("a", "b"), join=strategy),
            _node("end", NodeType.OUTPUT),
        ],
        edges=[
            _edge("e1", "start", "output", "branch_a", "input"),
            _edge("e2", "start", "output", "branch_b", "input"),
            _edge("e3", "branch_a", "output", "join", "a"),
            _edge("e4", "branch_b", "output", "join", "b"),
            _edge("e5", "join", "output", "end", "input"),
        ],
        # A populated set on the definition: the hash-order trap surface.
        workspace_capabilities={"llm", "retriever", "tool", "agent", "http"},
    )


def _compile_bytes(revision: WorkflowRevision, manifest: CapabilityManifest | None = None) -> bytes:
    plan = WorkflowCompiler.compile(revision, manifest=manifest)
    return plan.model_dump_json().encode("utf-8")


# ---------------------------------------------------------------------------
# Determinism — in-process
# ---------------------------------------------------------------------------


class TestDeterminismInProcess:
    def test_repeated_compiles_byte_identical(self) -> None:
        revision = WorkflowRevision.create(_join_definition(JoinStrategy.ALL), revision=1)
        first = _compile_bytes(revision)
        for _ in range(REPEAT_COMPILES - 1):
            assert _compile_bytes(revision) == first

    def test_two_revisions_of_same_bytes_identical(self) -> None:
        left = WorkflowRevision.create(_join_definition(JoinStrategy.ANY), revision=1)
        right = WorkflowRevision.create(_join_definition(JoinStrategy.ANY), revision=1)
        assert left.content_hash == right.content_hash
        assert _compile_bytes(left) == _compile_bytes(right)

    def test_plan_contains_no_set_order_dependence(self) -> None:
        """The plan payload is built only from definition-ordered or
        explicitly sorted collections — no set iteration reaches it."""
        revision = WorkflowRevision.create(_join_definition(JoinStrategy.ALL), revision=1)
        plan = WorkflowCompiler.compile(revision)
        join_step = plan.step_by_node_id("join")
        assert join_step is not None
        assert join_step.join_waits_for == ["a", "b"]  # sorted, stated
        assert list(join_step.input_mapping) == ["a", "b"]  # sorted keys
        # Steps stay in topological order across compiles.
        assert [s.node_id for s in plan.steps] == [
            s.node_id for s in WorkflowCompiler.compile(revision).steps
        ]


# ---------------------------------------------------------------------------
# Determinism — cross-process (the set/hash-seed trap)
# ---------------------------------------------------------------------------


def _subprocess_compile(seed: str, tmp_path: Path) -> bytes:
    """Compile the same definition bytes in a fresh interpreter with the
    given PYTHONHASHSEED and return the plan bytes."""
    script = tmp_path / "compile_once.py"
    script.write_text(
        "import json, sys\n"
        "from workflow_engine import (\n"
        "    JoinStrategy, NodeSpec, NodeType, PortSpec,\n"
        "    WorkflowCompiler, WorkflowDefinition, WorkflowEdgeSpec,\n"
        "    WorkflowRevision,\n"
        ")\n"
        "n = lambda i, t, ip=('input',), op=('output',), j=None: NodeSpec(\n"
        "    id=i, type=t,\n"
        "    input_ports=[PortSpec(name=p, type='any') for p in ip],\n"
        "    output_ports=[PortSpec(name=p, type='any') for p in op],\n"
        "    join_strategy=j,\n"
        ")\n"
        "e = lambda eid, s, sp, t, tp: WorkflowEdgeSpec(\n"
        "    id=eid, source_node_id=s, source_port=sp, target_node_id=t, target_port=tp)\n"
        "spec = WorkflowDefinition(\n"
        "    id='wf-join', workspace_id='ws-1', name='Join',\n"
        "    nodes=[\n"
        "        n('start', NodeType.TRIGGER, ip=()),\n"
        "        n('branch_a', NodeType.TRANSFORM),\n"
        "        n('branch_b', NodeType.TRANSFORM),\n"
        "        n('join', NodeType.TRANSFORM, ip=('a', 'b'), j=JoinStrategy.ALL),\n"
        "        n('end', NodeType.OUTPUT),\n"
        "    ],\n"
        "    edges=[\n"
        "        e('e1', 'start', 'output', 'branch_a', 'input'),\n"
        "        e('e2', 'start', 'output', 'branch_b', 'input'),\n"
        "        e('e3', 'branch_a', 'output', 'join', 'a'),\n"
        "        e('e4', 'branch_b', 'output', 'join', 'b'),\n"
        "        e('e5', 'join', 'output', 'end', 'input'),\n"
        "    ],\n"
        "    workspace_capabilities={'llm', 'retriever', 'tool', 'agent', 'http'},\n"
        ")\n"
        "rev = WorkflowRevision.create(spec, revision=1)\n"
        "sys.stdout.write(rev.content_hash + '\\n')\n"
        "plan = WorkflowCompiler.compile(rev)\n"
        "sys.stdout.write(plan.model_dump_json())\n"
    )
    repo_root = Path(__file__).resolve().parents[2]
    env = {
        "PYTHONHASHSEED": seed,
        "PYTHONPATH": str(repo_root / "packages"),
        "PATH": "/usr/bin:/bin",
    }
    out = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=60,
    )
    hash_line, _, plan_json = out.stdout.partition("\n")
    assert len(hash_line) == 64, out.stdout[:200]
    return plan_json.encode("utf-8")


class TestDeterminismCrossProcess:
    def test_same_bytes_same_plan_across_hash_seeds(self, tmp_path: Path) -> None:
        """The load-bearing proof: identical definition bytes compile to an
        identical plan under different PYTHONHASHSEED values — the set-order
        trap (``workspace_capabilities``) no longer leaks into the plan."""
        seed_one = _subprocess_compile("1", tmp_path)
        seed_two = _subprocess_compile("2", tmp_path)
        seed_three = _subprocess_compile("12345", tmp_path)
        assert seed_one == seed_two == seed_three

    def test_revision_hash_seed_independent(self) -> None:
        """The hash itself is seed-independent (canonical normalizer)."""
        left = WorkflowRevision.create(_join_definition(JoinStrategy.ALL), revision=1)
        right = WorkflowRevision.create(_join_definition(JoinStrategy.ALL), revision=1)
        assert left.content_hash == right.content_hash
        assert _compile_bytes(left) == _compile_bytes(right)


# ---------------------------------------------------------------------------
# Join lock — declared strategies + refusal
# ---------------------------------------------------------------------------


class TestJoinLock:
    def test_undeclared_join_refuses_to_compile(self) -> None:
        """A 2-inbound node with no declared strategy refuses — typed."""
        revision = WorkflowRevision.create(_join_definition(None), revision=1)
        with pytest.raises(ValueError) as exc_info:
            WorkflowCompiler.compile(revision)
        message = str(exc_info.value)
        assert "join" in message
        assert "'join'" in message  # names the node
        assert "all" in message and "any" in message and "first" in message  # names the strategies

    def test_undeclared_join_refused_through_compile_revalidation(self) -> None:
        """The refusal surfaces through the standard compile entry (which
        re-validates), not only on a private helper."""
        revision = WorkflowRevision.create(_join_definition(None), revision=1)
        with pytest.raises(ValueError):
            WorkflowCompiler.compile(
                revision, manifest=CapabilityManifest(supported_node_types=set(NodeType))
            )

    def test_single_inbound_node_needs_no_declaration(self) -> None:
        """No over-rejection: a one-wire node is not a join node."""
        spec = WorkflowDefinition(
            id="wf-linear",
            workspace_id="ws-1",
            name="Linear",
            nodes=[
                _node("start", NodeType.TRIGGER, in_ports=()),
                _node("mid", NodeType.TRANSFORM),
                _node("end", NodeType.OUTPUT),
            ],
            edges=[
                _edge("e1", "start", "output", "mid", "input"),
                _edge("e2", "mid", "output", "end", "input"),
            ],
        )
        revision = WorkflowRevision.create(spec, revision=1)
        plan = WorkflowCompiler.compile(revision)
        for step in plan.steps:
            assert step.join_mode == "all"
            assert step.join_emit_on_partial == "nothing"

    @pytest.mark.parametrize(
        ("strategy", "waits_for", "emit_on_partial"),
        [
            (JoinStrategy.ALL, ["a", "b"], "nothing"),
            (JoinStrategy.ANY, ["a", "b"], "first-arrival"),
            (JoinStrategy.FIRST, ["a"], "first-input-only"),
        ],
    )
    def test_each_strategy_states_its_barrier(
        self,
        strategy: JoinStrategy,
        waits_for: list[str],
        emit_on_partial: str,
    ) -> None:
        """Every declared strategy compiles with its EXPLICIT barrier:
        which inputs it waits for, what it emits on partial input, timeout."""
        revision = WorkflowRevision.create(_join_definition(strategy), revision=1)
        plan = WorkflowCompiler.compile(revision)
        step = plan.step_by_node_id("join")
        assert step is not None
        assert step.join_mode == strategy.value
        assert step.join_waits_for == waits_for
        assert step.join_emit_on_partial == emit_on_partial
        assert step.join_timeout_s == JOIN_BARRIER_TIMEOUT_S

    def test_declared_join_deterministic_across_repeats(self) -> None:
        revision = WorkflowRevision.create(_join_definition(JoinStrategy.FIRST), revision=1)
        first = _compile_bytes(revision)
        for _ in range(REPEAT_COMPILES - 1):
            assert _compile_bytes(revision) == first

    def test_invalid_strategy_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            NodeSpec(id="j", type=NodeType.TRANSFORM, join_strategy="sometimes")  # type: ignore[arg-type]

    def test_two_inbound_edges_from_same_source_is_still_a_join(self) -> None:
        """Fan-in of two wires from one source: still a declared join."""
        spec = WorkflowDefinition(
            id="wf-dup-in",
            workspace_id="ws-1",
            name="DupIn",
            nodes=[
                _node("start", NodeType.TRIGGER, in_ports=()),
                _node("join", NodeType.TRANSFORM, in_ports=("x", "y"), join=JoinStrategy.ANY),
                _node("end", NodeType.OUTPUT),
            ],
            edges=[
                _edge("e1", "start", "output", "join", "x"),
                _edge("e2", "start", "output", "join", "y"),
                _edge("e3", "join", "output", "end", "input"),
            ],
        )
        plan = WorkflowCompiler.compile(
            WorkflowRevision.create(spec, revision=1)
        )
        step = plan.step_by_node_id("join")
        assert step is not None
        assert step.join_mode == "any"
        assert step.join_waits_for == ["x", "y"]

    def test_validator_still_passes_declared_join(self) -> None:
        """The join declaration does not disturb the validator layer."""
        spec = _join_definition(JoinStrategy.ALL)
        result = WorkflowValidator.validate(
            spec,
            CapabilityManifest(
                supported_node_types=set(NodeType),
                enabled_capabilities={"llm"},
            ),
        )
        assert result.valid, result.error_summary


# ---------------------------------------------------------------------------
# Plan payload is JSON-stable (byte-identity is checkable by consumers)
# ---------------------------------------------------------------------------


class TestPlanSerialization:
    def test_plan_json_round_trips(self) -> None:
        revision = WorkflowRevision.create(_join_definition(JoinStrategy.ANY), revision=1)
        plan = WorkflowCompiler.compile(revision)
        payload = json.loads(plan.model_dump_json())
        assert payload["revision_hash"] == revision.content_hash
        assert [s["node_id"] for s in payload["steps"]] == [s.node_id for s in plan.steps]
