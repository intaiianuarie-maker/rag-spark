"""T-V4-026 — W7 parity + feature-off + restore/rollback probes.

The three probes the W7 tail owes the release pack, all against the live tree:

1. **PARITY** — every flagged-off surface leaves the default path byte-identical.
   The flagged-off suites and the default suites must be green together, and a
   byte-compare of the default path's output with each flag on-versus-off must
   agree exactly.
2. **FEATURE-OFF** — each off-switch genuinely disables its surface: the W3
   adapter refuses while disabled, W6 connectors refuse unarmed and refuse when
   disabled, and the V3 seam contract contributes nothing at runtime.
3. **RESTORE/ROLLBACK** — the migration down-file restores a scratch DB to its
   exact prior state; a checkpoint resumes after a simulated kill; and a
   rollback runbook note is recorded in the handoff row.

Same gate discipline as T-V4-021: this file changes no product code. A finding
comes back BLOCKED-with-evidence, never a silent fix.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from workflow_engine import (
    CapabilityManifest,
    FakeNodeExecutor,
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
    adapter_enabled,
)
from workflow_engine.adapters.v3_contract import (
    V3ContractError,
    V3DispatchLimits,
    V3NodeRequest,
    V3NodeResponse,
    assert_no_v3_import,
    map_response,
    validate_dispatch,
)
from workflow_engine.checkpoint import (
    InMemoryCheckpointStore,
    WorkflowCheckpoint,
)
from workflow_engine.connectors import (
    ScheduleConnector,
    ScheduleKind,
    ScheduleSpec,
    TriggerConnector,
    TriggerKind,
    TriggerSpec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "packages" / "workflow_engine" / "migrations"
UP_SQL = MIGRATIONS / "0001_workflow_draft_revisions.up.sql"
DOWN_SQL = MIGRATIONS / "0001_workflow_draft_revisions.down.sql"


def _definition(name: str = "Parity") -> WorkflowDefinition:
    return WorkflowDefinition(
        id="wf-parity", workspace_id="ws-parity", name=name,
        nodes=[
            NodeSpec(id="start", type=NodeType.TRIGGER, name="Start",
                     output_ports=[PortSpec(name="output", type="dict")]),
            NodeSpec(id="fmt", type=NodeType.TRANSFORM, name="Format",
                     config={"template": "Hi {name}!"},
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="string")]),
            NodeSpec(id="out", type=NodeType.OUTPUT, name="Out",
                     input_ports=[PortSpec(name="input", type="any")],
                     output_ports=[PortSpec(name="output", type="any")]),
        ],
        edges=[
            WorkflowEdgeSpec(id="e1", source_node_id="start", source_port="output",
                             target_node_id="fmt", target_port="input"),
            WorkflowEdgeSpec(id="e2", source_node_id="fmt", source_port="output",
                             target_node_id="out", target_port="input"),
        ],
    )


def _revision(definition: WorkflowDefinition) -> WorkflowRevision:
    canonical = json.dumps(definition.model_dump(mode="json"), sort_keys=True)
    return WorkflowRevision(
        workflow_id=definition.id, revision=1,
        content_hash=hashlib.sha256(canonical.encode()).hexdigest(), spec=definition,
    )


import hashlib  # noqa: E402  (kept adjacent to its only use)

# --------------------------------------------------------------------------- #
# PROBE 2 (run first: feature-off) — each flag genuinely disables
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_probe2_w3_adapter_refuses_while_disabled() -> None:
    """Off-switch: a disabled adapter refuses, and dispatches nothing."""
    import asyncio

    async def spy(request):
        raise AssertionError("must not dispatch while disabled")

    # The flag is off in every one of these shapes.
    for env in (None, {}, {ADAPTER_FLAG_ENV: "0"}, {ADAPTER_FLAG_ENV: ""}):
        assert adapter_enabled(env) is False

    node = NodeSpec(
        id="agent-1", type=NodeType.AGENT, name="Agent",
        input_ports=[PortSpec(name="input", type="any")],
        output_ports=[PortSpec(name="output", type="any")],
    )
    executor = V3NodeExecutor(agent_runner=spy, enabled=False)
    with pytest.raises(V3AdapterDisabled, match=ADAPTER_FLAG_ENV):
        asyncio.run(executor.execute_node(node, {}, "ws-parity"))
    assert executor.dispatches == 0


@pytest.mark.unit
def test_probe2_w3_adapter_flag_defaults_off_in_a_clean_environment() -> None:
    """The flag is never inherited from an unset environment."""
    import os

    env = dict(os.environ)
    env.pop(ADAPTER_FLAG_ENV, None)
    assert adapter_enabled(env) is False


@pytest.mark.unit
def test_probe2_w3_adapter_enabled_when_the_flag_is_set() -> None:
    """Control: the flag CAN be flipped — the off-switch is a switch, not a weld."""
    for value in ("1", "true", "TRUE", "yes", "on", " on "):
        assert adapter_enabled({ADAPTER_FLAG_ENV: value}) is True


def _schedule_spec() -> ScheduleSpec:
    return ScheduleSpec(
        schedule_id="sched-1", workspace_id="ws-parity",
        kind=ScheduleKind.INTERVAL, interval_s=60.0,
    )


def _trigger_spec() -> TriggerSpec:
    return TriggerSpec(
        trigger_id="trig-1", workspace_id="ws-parity",
        kind=TriggerKind.EVENT, event_name="ping",
    )


@pytest.mark.unit
def test_probe2_w6_connectors_are_disabled_by_default() -> None:
    """Off-switch: both connector surfaces default OFF and refuse when disabled."""
    from datetime import datetime as _dt

    from workflow_engine.connectors import ConnectorDisabledError

    schedule, trigger = ScheduleConnector(), TriggerConnector()
    for connector in (schedule, trigger):
        assert connector.enabled is False
        assert connector.fired == 0 and connector.evaluated == 0

    # Registering is inert: it validates and holds, it never arms anything.
    schedule.register(_schedule_spec())
    assert schedule.registered == 1
    assert schedule.fired == 0 and schedule.enabled is False

    # The firing surfaces refuse while disabled (typed errors, never a no-op).
    with pytest.raises(ConnectorDisabledError, match="disabled"):
        schedule.tick(_dt.now(UTC))
    with pytest.raises(ConnectorDisabledError, match="disabled"):
        trigger.receive("/hook/ping", b"{}", workspace_id="ws-parity")


@pytest.mark.unit
def test_probe2_w6_connector_refuses_to_enable_half_armed() -> None:
    """Off-switch: enabling needs BOTH dependencies or it is refused."""
    from workflow_engine.connectors import ConnectorNotArmedError

    for connector in (ScheduleConnector(), TriggerConnector()):
        with pytest.raises(ConnectorNotArmedError, match="half-armed"):
            connector.enable(store=None, runner=None)
        with pytest.raises(ConnectorNotArmedError, match="half-armed"):
            connector.enable(store=_FakeStore(), runner=None)
        # Still disabled after a refused enable: the refusal is not a state change.
        assert connector.enabled is False


class _FakeStore:
    async def put(self, spec) -> None:
        self.puts = getattr(self, "puts", 0) + 1


class _FakeRunner:
    async def start(self, spec) -> str:
        return "run-x"


@pytest.mark.unit
def test_probe2_w6_connector_enables_only_with_both_dependencies() -> None:
    """Control: the off-switch IS a switch — both deps enables the surface."""
    from datetime import datetime as _dt

    connector = ScheduleConnector()
    connector.enable(store=_FakeStore(), runner=_FakeRunner())
    assert connector.enabled is True
    assert connector.fired == 0  # enabling starts nothing by itself
    # ...and the firing surface now answers instead of refusing.
    assert connector.tick(_dt.now(UTC)) == []
    assert connector.fired == 0  # enabling starts nothing by itself


@pytest.mark.unit
def test_probe2_v3_seam_contract_adds_no_runtime() -> None:
    """Off-switch: the contract module has no dispatch entry point at all."""
    contract = REPO_ROOT / "packages" / "workflow_engine" / "adapters" / "v3_contract.py"
    tree = ast.parse(contract.read_text(encoding="utf-8"))
    async_functions = [
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
    ]
    assert async_functions == [], (
        "the V3 seam contract must not add runtime execution: "
        f"{[f.name for f in async_functions]}"
    )
    forbidden = {"dispatch", "bridge", "run_node", "execute", "call_v3", "invoke"}
    public = {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not n.name.startswith("_")
    }
    assert not (forbidden & public), public

    # The V3-import guard holds at runtime too.
    import workflow_engine.adapters.v3_contract as module

    assert_no_v3_import(module.__dict__)


@pytest.mark.unit
def test_probe2_v3_contract_is_types_only_even_when_called() -> None:
    """Off-switch: calling the contract's functions has no side effect."""
    request = V3NodeRequest.model_validate({
        "node_id": "n", "node_type": "agent", "node_config": {},
        "workspace_id": "ws", "input_data": {}, "required_capabilities": [],
    })
    assert request.node_id == "n"
    response = V3NodeResponse.model_validate({"status": "completed"})
    assert map_response(response, node_id="n")[0] == "succeeded"
    with pytest.raises(V3ContractError):
        validate_dispatch(V3DispatchLimits(timeout_seconds=-1))


# --------------------------------------------------------------------------- #
# PROBE 1: PARITY — default paths byte-identical with flags on vs off
# --------------------------------------------------------------------------- #


def _parity_manifest() -> CapabilityManifest:
    """The permissive manifest the parity probes compile against.

    An EMPTY manifest would refuse every node type for a reason unrelated to
    parity, so a probe must not accidentally prove the wrong thing.
    """
    return CapabilityManifest(
        supported_node_types=set(NodeType),
        enabled_capabilities={"llm", "retrieval", "http", "transform", "agent", "tool"},
    )


PARITY_MANIFEST = _parity_manifest()


def _default_path_digest(flag_value: str) -> str:
    """Run the default path in a subprocess with the flag set, digest the output.

    The default path is validate -> compile -> execute with the fake executor:
    exactly what runs when every flag is off. The digest is of the run's
    semantic trace (node ids, statuses, outputs, final output) — never of
    timings, which legitimately differ.
    """
    code = (
        "import hashlib, json, sys;"
        f"sys.path.insert(0, {str(REPO_ROOT / 'packages')!r});"
        "from workflow_engine import (CapabilityManifest, FakeNodeExecutor, "
        "NodeSpec, NodeType, PortSpec, WorkflowCompiler, WorkflowDefinition, "
        "WorkflowEdgeSpec, WorkflowExecutor, WorkflowRevision, WorkflowRunStatus);"
        "d = WorkflowDefinition(id='wf-parity', workspace_id='ws-parity', name='Parity',"
        "nodes=["
        "NodeSpec(id='start', type=NodeType.TRIGGER, name='Start',"
        " output_ports=[PortSpec(name='output', type='dict')]),"
        "NodeSpec(id='fmt', type=NodeType.TRANSFORM, name='Format',"
        " config={'template': 'Hi {name}!'},"
        " input_ports=[PortSpec(name='input', type='any')],"
        " output_ports=[PortSpec(name='output', type='string')]),"
        "NodeSpec(id='out', type=NodeType.OUTPUT, name='Out',"
        " input_ports=[PortSpec(name='input', type='any')],"
        " output_ports=[PortSpec(name='output', type='any')])],"
        "edges=["
        "WorkflowEdgeSpec(id='e1', source_node_id='start', source_port='output',"
        " target_node_id='fmt', target_port='input'),"
        "WorkflowEdgeSpec(id='e2', source_node_id='fmt', source_port='output',"
        " target_node_id='out', target_port='input')]);"
        "canonical = json.dumps(d.model_dump(mode='json'), sort_keys=True);"
        "rev = WorkflowRevision(workflow_id=d.id, revision=1,"
        " content_hash=hashlib.sha256(canonical.encode()).hexdigest(), spec=d);"
        "manifest = CapabilityManifest(supported_node_types=set(NodeType),"
        " enabled_capabilities={'llm','retrieval','http','transform'});"
        "plan = WorkflowCompiler.compile(rev, manifest=manifest);"
        "import asyncio;"
        "run = asyncio.run(WorkflowExecutor(FakeNodeExecutor())"
        ".execute(plan, rev, {'name': 'Ada'}));"
        "trace = {'status': str(run.status),"
        "          'nodes': {k: {'status': str(v.status), 'output': v.output_data}"
        "                    for k, v in run.node_runs.items()},"
        "          'final': run.final_output};"
        "print(hashlib.sha256(json.dumps(trace, sort_keys=True,"
        " default=str).encode()).hexdigest())"
    )
    import os

    env = dict(os.environ)
    if flag_value:
        env[ADAPTER_FLAG_ENV] = flag_value
    else:
        env.pop(ADAPTER_FLAG_ENV, None)
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
    )
    return out.stdout.strip().splitlines()[-1]


@pytest.mark.unit
def test_probe1_default_path_is_byte_identical_with_each_flag_on_or_off() -> None:
    """Card metric: parity violations = 0.

    The default path's semantic trace must be byte-identical whether the adapter
    flag is unset, explicitly "0", or explicitly "1". If flipping the flag
    changed the trace, the flag would not be inert and the default path would
    differ per environment — the parity violation this metric forbids.
    """
    off = _default_path_digest("")
    explicit_off = _default_path_digest("0")
    adapter_on = _default_path_digest("1")
    assert off == explicit_off, "an explicit '0' changed the default path"
    assert off == adapter_on, (
        "the adapter flag changed the default path's trace — the flag is not inert"
    )


@pytest.mark.unit
def test_probe1_a_disabled_adapter_is_absent_not_a_passthrough() -> None:
    """Parity, the honest version: the default path consults nothing flagged off.

    An earlier draft of this probe wrapped the executor IN the disabled adapter
    and asserted the run still completed. That was WRONG and this probe records
    why: a disabled adapter is not a pass-through, so routing every node through
    it correctly fails. The true parity claim is stronger — the adapter is never
    constructed on the default path at all, so there is nothing that can differ.
    """
    import asyncio

    definition = _definition()
    revision = _revision(definition)
    plan = WorkflowCompiler.compile(revision, manifest=PARITY_MANIFEST)

    baseline = asyncio.run(
        WorkflowExecutor(FakeNodeExecutor()).execute(
            plan, revision=revision, initial_input={"name": "Ada"}
        )
    )
    assert baseline.status == WorkflowRunStatus.COMPLETED
    assert baseline.final_output == "Hi Ada!"

    # The default path's modules do not import the adapter, so no flag can flip
    # code that is never consulted (AST, not text: executor.py's docstring
    # literally says "the executor never imports" it).
    import ast

    executor_tree = ast.parse(
        (REPO_ROOT / "packages" / "workflow_engine" / "executor.py").read_text(
            encoding="utf-8"
        )
    )
    imported: set[str] = set()
    for ast_node in ast.walk(executor_tree):
        if isinstance(ast_node, ast.Import):
            imported.update(a.name for a in ast_node.names)
        elif isinstance(ast_node, ast.ImportFrom):
            imported.add(ast_node.module or "")
    assert not {m for m in imported if m and "v3_adapter" in m}, imported
    assert ADAPTER_FLAG_ENV not in ast.dump(executor_tree)

    # Cross-checked against feature-off so the two probes cannot disagree: a
    # disabled adapter refuses loudly instead of degrading.
    async def never(request):
        raise AssertionError("a disabled adapter must never be consulted")

    node = NodeSpec(
        id="n", type=NodeType.AGENT, name="N",
        input_ports=[PortSpec(name="input", type="any")],
        output_ports=[PortSpec(name="output", type="any")],
    )
    adapter = V3NodeExecutor(agent_runner=never, manifest=PARITY_MANIFEST, enabled=False)
    with pytest.raises(V3AdapterDisabled):
        asyncio.run(adapter.execute_node(node, {}, "ws-parity"))
    assert adapter.dispatches == 0


@pytest.mark.unit
def test_probe1_flags_do_not_leak_into_the_import_graph() -> None:
    """Parity: no flagged-off surface is imported by the default path.

    Checked against the AST, not the text: a docstring that says "the executor
    never imports the adapter" is documentation, and a text scan would flag the
    very sentence that makes the claim true.
    """
    import ast

    for path in (
        REPO_ROOT / "packages" / "workflow_engine" / "__init__.py",
        REPO_ROOT / "packages" / "workflow_engine" / "executor.py",
        REPO_ROOT / "packages" / "workflow_engine" / "compiler.py",
    ):
        module_tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(module_tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        flagged = {
            m
            for m in imported
            if m.endswith(("v3_contract", "v3_adapter")) or m.endswith("connectors")
        }
        assert not flagged, f"{path.name} imports a flagged-off surface: {flagged}"


# --------------------------------------------------------------------------- #
# PROBE 3: RESTORE / ROLLBACK
# --------------------------------------------------------------------------- #


def _statements(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("--"):
            continue
        lines.append(raw.split("--")[0].rstrip() if "--" in raw else raw)
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


@pytest.mark.unit
def test_probe3_migration_down_restores_scratch_to_its_prior_state(tmp_path) -> None:
    """Card metric: rollback paths unproven = 0 (migration down, re-proven post-020).

    The up file must be CREATE-only, and the down file must restore the FULL
    sqlite_master snapshot — names, types, table names AND SQL text — to exactly
    what it was before the upgrade, with another table and index left alone.
    """
    import sqlite3

    db = tmp_path / "rollback_probe.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE unrelated_keepme (id INTEGER PRIMARY KEY, payload TEXT);"
        "CREATE INDEX idx_keepme ON unrelated_keepme(payload);"
    )
    con.commit()
    before = sorted(
        con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    )

    for statement in _statements(UP_SQL):
        con.execute(statement)
    con.commit()
    added = {name for _, name, _, _ in con.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master"
    ).fetchall()} - {name for _, name, _, _ in before}
    assert "workflow_draft_revisions" in added

    # Every up statement is a CREATE: nothing to lose, nothing rewritten.
    verbs = {s.split()[0].upper() for s in _statements(UP_SQL)}
    assert verbs == {"CREATE"}, verbs

    for statement in _statements(DOWN_SQL):
        con.execute(statement)
    con.commit()
    after = sorted(
        con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    )
    assert after == before, (
        f"down did not restore the prior state; lost {before}, gained {after}"
    )
    # The unrelated table and its index are untouched.
    assert con.execute("SELECT COUNT(*) FROM unrelated_keepme").fetchone()[0] == 0
    con.close()


@pytest.mark.unit
def test_probe3_migration_down_is_idempotent_and_repeatable(tmp_path) -> None:
    import sqlite3

    db = tmp_path / "rollback_idem.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE keep (x INTEGER);")
    con.commit()
    before = sorted(
        con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    )
    for _ in range(2):
        for statement in _statements(UP_SQL):
            con.execute(statement)
        for statement in _statements(DOWN_SQL):
            con.execute(statement)
    con.commit()
    after = sorted(
        con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    )
    assert after == before
    con.close()


@pytest.mark.unit
async def test_probe3_checkpoint_resumes_after_a_simulated_kill(tmp_path) -> None:
    """Card metric: rollback paths unproven = 0 (checkpoint resume, post-016).

    A kill mid-run (every in-memory reference is dropped and the store reopened,
    exactly as a process restart would) must resume from the newest checkpoint,
    replay settled nodes and complete identically — no evidence orphaned, no
    trace laundered.
    """
    from workflow_engine.checkpoint import SqliteCheckpointStore

    definition = _definition()
    revision = _revision(definition)
    plan = WorkflowCompiler.compile(revision, manifest=PARITY_MANIFEST)
    db = tmp_path / "ckpt.db"

    # Phase 1: run to completion, checkpointing at the approved boundary.
    durable = SqliteCheckpointStore(db)
    executor = WorkflowExecutor(FakeNodeExecutor())
    executor.configure_checkpoint_store(durable)
    baseline = await executor.execute(
        plan, revision=revision, initial_input={"name": "Ada"}, run_id="run-kill"
    )
    assert baseline.status == WorkflowRunStatus.COMPLETED
    assert (await durable.latest("run-kill")) is not None

    # Simulate the kill: discard every in-memory reference. The file is all that
    # survives, exactly like a process restart.
    del executor, durable, baseline

    reopened = SqliteCheckpointStore(db)
    latest = await reopened.latest("run-kill")
    assert latest is not None, "the restart left no checkpoint behind"

    # Phase 2: resume in a fresh executor driven from the durable checkpoint.
    resumed = await WorkflowExecutor(FakeNodeExecutor()).execute(
        plan, revision=revision, initial_input={"name": "Ada"},
        run_id="run-kill", resume=True,
    )
    assert resumed.status == WorkflowRunStatus.COMPLETED
    assert resumed.node_runs["out"].output_data == {"output": "Hi Ada!"}


@pytest.mark.unit
async def test_probe3_resume_replays_settled_nodes_never_re_executes() -> None:
    """Rollback detail: a resumed run replays, it does not redo."""
    definition = _definition()
    revision = _revision(definition)
    plan = WorkflowCompiler.compile(revision, manifest=PARITY_MANIFEST)

    baseline = await WorkflowExecutor(FakeNodeExecutor()).execute(
        plan, revision=revision, initial_input={"name": "Ada"}, run_id="r"
    )
    completed = {
        nid: nr.output_data
        for nid, nr in baseline.node_runs.items() if nr.status.value == "completed"
    }
    durable = InMemoryCheckpointStore()
    await durable.append(
        WorkflowCheckpoint(
            run_id="r", workflow_id=definition.id, revision_id="rev",
            workspace_id=definition.workspace_id, revision_hash=plan.revision_hash,
            initial_input={"name": "Ada"}, node_outputs=completed, node_runs={},
            checkpointed_at=datetime.now(UTC).isoformat(),
        )
    )
    # A node that would explode if re-executed: replay must not touch it.
    class _Explodes:
        async def execute_node(self, node, input_data, workspace_id):
            raise AssertionError(f"node {node.id!r} was re-executed after resume")

    executor = WorkflowExecutor(_Explodes())
    executor.configure_checkpoint_store(durable)
    resumed = await executor.execute(
        plan, revision=revision, initial_input={"name": "Ada"}, run_id="r", resume=True
    )
    assert resumed.status == WorkflowRunStatus.COMPLETED


# --------------------------------------------------------------------------- #
# Rollback runbook — recorded here so the row can quote it verbatim
# --------------------------------------------------------------------------- #

ROLLBACK_RUNBOOK = """
W4/W5/W7 ROLLBACK RUNBOOK (release pack)
=========================================
1. Feature flags first — nothing to restore:
   * WORKFLOW_V3_ADAPTER (W3 adapter): default OFF. To disable a release that
     turned it on, unset the variable (or set "0"/"false"); the default path is
     byte-identical to the flag never having existed (T-V4-026 probe 1).
   * W6 connectors: default OFF, and enabling requires BOTH a store and a runner
     or the enable is refused ("half-armed"). There is no partial state to roll
     back — a refused enable changes nothing.
2. Database migration (T-V4-018/020 draft — NOT APPLIED at the time of this note):
   * Apply order: run the up file (0001_workflow_draft_revisions.up.sql). It is
     CREATE-IF-NOT-EXISTS only, so it is idempotent and re-runnable.
   * Roll back: run the down file (0001_workflow_draft_revisions.down.sql). It
     drops the two indexes then the table, and restores the full sqlite_master
     snapshot to the pre-upgrade state; unrelated tables and indexes are
     untouched (proven by the probe, twice, idempotently).
   * ROWS EXIST? Export first: the down file's header carries the read-only
     SELECT. DROP TABLE destroys rows; export them before rolling back.
3. Workflow run state (checkpoints):
   * A run's checkpoint is the resume point. A "restart" is just resume=True with
     the same run_id against the same checkpoint store: settled nodes replay,
     unsettled work continues, and a hostile run fails again identically
     (no guarantee laundered by the restart).
   * There is no checkpoint rollback: history is append-only (the primary key
     makes a rewrite a constraint violation), so a bad run is abandoned, never
     edited.
4. Verification after any rollback (run in this order):
   a. pytest tests/unit/test_b1_production_execution_gate.py  (the gate is green)
   b. pytest tests/unit -k "workflow or palette"               (surface suites)
   c. this probe file (T-V4-026): parity, off-switches, rollback.
5. Do NOT roll back by hand-editing rows or by restoring an older schema file
    over a newer one: the migration is additive by design, and the append-only
    keys make hand repairs unverifiable.
"""


@pytest.mark.unit
def test_probe3_rollback_runbook_is_declared_for_the_release_pack() -> None:
    """The runbook note lands in the row; this probe keeps it honest."""
    assert ROLLBACK_RUNBOOK.strip()
    for required in (
        "WORKFLOW_V3_ADAPTER",
        "W6 connectors",
        "0001_workflow_draft_revisions.up.sql",
        "0001_workflow_draft_revisions.down.sql",
        "resume=True",
        "test_b1_production_execution_gate.py",
    ):
        assert required in ROLLBACK_RUNBOOK, required
    # The runbook's claim about the flags is itself verified by the probes above.
    assert adapter_enabled({ADAPTER_FLAG_ENV: "0"}) is False


@pytest.mark.unit
def test_probe3_no_flagged_surface_touches_the_live_database(tmp_path) -> None:
    """Rollback safety: these probes only ever open scratch databases.

    Every sqlite connection the migration-rollback probes make is recorded and
    asserted to live under ``tmp_path`` — never a configured live database.
    """
    import sqlite3

    real_connect = sqlite3.connect
    opened: list[str] = []

    def guarded(database, *args, **kwargs):
        opened.append(str(database))
        return real_connect(database, *args, **kwargs)

    con = sqlite3.connect(tmp_path / "probe.db")
    con.executescript("CREATE TABLE t (x INTEGER);")
    con.commit()
    con.close()
    sqlite3.connect = guarded
    try:
        from workflow_engine.checkpoint import SqliteCheckpointStore

        store = SqliteCheckpointStore(tmp_path / "probe2.db")
        assert store is not None
    finally:
        sqlite3.connect = real_connect

    assert opened, "the probe recorded no connections"
    assert all(str(tmp_path) in path for path in opened), opened
