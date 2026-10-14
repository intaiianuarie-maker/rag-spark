"""V4 release pack as DATA (T-V4-027) — envelope, flags, rollback.

Three tables, asserted against the live tree by ``test_workflow_release_pack``:

* :data:`RESOURCE_ENVELOPE` — every bound the V4 runtime enforces (validator
  caps, per-node bounds, run envelope, import-time guards), each with its
  default, its layer and its reason.  The default in this table is the single
  restated literal in the tree: the test asserts the live constant equals it,
  so a constant changed without updating the pack FAILS the test.
* :data:`FLAG_REGISTRY` — every feature flag with its default state, its
  enable preconditions and the refusal that holds while off.  Every flag in
  the package appears here; the test scans for flag constants and fails on an
  undocumented one.
* :data:`ROLLBACK_NOTE` — ordered rollback steps per surface (migration
  down-file, checkpoint resume, flag-off), each naming the ticket that proved
  it and a re-run pointer.

This module is DATA + prose only: it changes no product behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# 1. RESOURCE ENVELOPE — every bound, stated with default + reason
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EnvelopeBound:
    """One enforced bound.  ``path`` is the live dotted constant path."""

    path: str
    default: int | float
    layer: str
    reason: str


RESOURCE_ENVELOPE: tuple[EnvelopeBound, ...] = (
    # --- validator (T-V4-006): definition-level resource caps -------------
    EnvelopeBound(
        path="workflow_engine.validator.MAX_GRAPH_NODES",
        default=200,
        layer="validator",
        reason="graph node cap; structural checks are quadratic in it",
    ),
    EnvelopeBound(
        path="workflow_engine.validator.MAX_GRAPH_EDGES",
        default=500,
        layer="validator",
        reason="total edge cap; mirrors the import-time cap (one policy)",
    ),
    EnvelopeBound(
        path="workflow_engine.validator.MAX_EDGES_PER_NODE",
        default=64,
        layer="validator",
        reason="per-node fan-out; catalog nodes declare <=2 output ports",
    ),
    EnvelopeBound(
        path="workflow_engine.validator.MAX_CONFIG_BYTES_PER_NODE",
        default=32_768,
        layer="validator",
        reason="per-node config bytes; import caps per-field length, not the sum",
    ),
    EnvelopeBound(
        path="workflow_engine.validator.MAX_CONFIG_BYTES_TOTAL",
        default=1_048_576,
        layer="validator",
        reason="definition-wide config bytes; bounds the revision-hash payload",
    ),
    # --- executor node layer (T-V4-010) -----------------------------------
    EnvelopeBound(
        path="workflow_engine.executor.NODE_TIMEOUT_S",
        default=30.0,
        layer="executor.node",
        reason="per-node wall-clock; a hung node fails the node, not the run",
    ),
    EnvelopeBound(
        path="workflow_engine.executor.MAX_NODE_OUTPUT_BYTES",
        default=1_048_576,
        layer="executor.node",
        reason="per-node output cap; refused before store/propagate/hash",
    ),
    EnvelopeBound(
        path="workflow_engine.executor.NODE_MAX_ATTEMPTS",
        default=3,
        layer="executor.node",
        reason="retry cap; a permanently failing node fails loudly",
    ),
    EnvelopeBound(
        path="workflow_engine.executor.NODE_RETRY_BASE_S",
        default=0.05,
        layer="executor.node",
        reason="bounded exponential backoff base; total delay <= 0.15 s",
    ),
    # --- executor run layer (T-V4-012) ------------------------------------
    EnvelopeBound(
        path="workflow_engine.executor.RUN_DEADLINE_S",
        default=300.0,
        layer="executor.run",
        reason="run-wide wall-clock; expiry cancels cleanly, never hangs",
    ),
    EnvelopeBound(
        path="workflow_engine.executor.RUN_MAX_NODE_SECONDS",
        default=600.0,
        layer="executor.run",
        reason="run-wide node-seconds; 2x the deadline, bounds total compute",
    ),
    EnvelopeBound(
        path="workflow_engine.executor.RUN_MAX_NODES",
        default=200,
        layer="executor.run",
        reason="executed-node accounting cap; parity with the validator cap",
    ),
    # --- import-time guards (W2-1 / B-1) ----------------------------------
    EnvelopeBound(
        path="workflow_engine.adapters.langflow_adapter.LangflowAdapter.MAX_NODES",
        default=200,
        layer="import",
        reason="untrusted-JSON node cap; refuses before translation work",
    ),
    EnvelopeBound(
        path="workflow_engine.adapters.langflow_adapter.LangflowAdapter.MAX_EDGES",
        default=500,
        layer="import",
        reason="untrusted-JSON edge cap; parity with the validator cap",
    ),
    EnvelopeBound(
        path="workflow_engine.adapters.langflow_adapter.LangflowAdapter.MAX_FIELD_CHARS",
        default=50_000,
        layer="import",
        reason="per-field length cap at ingestion (F-5 class)",
    ),
    EnvelopeBound(
        path="workflow_engine.adapters.langflow_adapter.MAX_DEPTH",
        default=100,
        layer="import",
        reason="flow-structure scan depth; an iterative scan cannot recurse",
    ),
    EnvelopeBound(
        path="workflow_engine.adapters.langflow_adapter.MAX_TEMPLATE_DEPTH",
        default=32,
        layer="import",
        reason="value/default chain depth; a cycle or deep chain is refused",
    ),
)


# --------------------------------------------------------------------------- #
# 2. FLAG REGISTRY — every flag, default state, preconditions, refusal
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FlagEntry:
    """One feature flag.  Default state is OFF for every V4 surface."""

    flag: str
    surface: str
    default_state: str
    enable_preconditions: tuple[str, ...]
    refusal_while_off: str
    refusal_proof: str
    proving_ticket: str


FLAG_REGISTRY: tuple[FlagEntry, ...] = (
    FlagEntry(
        flag="WORKFLOW_V3_ADAPTER",
        surface="W3 LangGraph adapter (AGENT node execution)",
        default_state="off",
        enable_preconditions=(
            "env var set truthy (1/true/yes/on, case-insensitive, trimmed)",
            "agent_runner injected (an AgentRunner Protocol callable)",
            "capability manifest supplied (default permissive CapabilityManifest)",
        ),
        refusal_while_off="V3AdapterDisabled; dispatches stay 0, the runner is never touched",
        refusal_proof="V3NodeExecutor(enabled=False).execute_node raises V3AdapterDisabled "
        "with adapter.dispatches == 0",
        proving_ticket="T-V4-015",
    ),
    FlagEntry(
        flag="WORKFLOW_V3_ADAPTER (flag-on, no runner)",
        surface="W3 adapter configuration error",
        default_state="off",
        enable_preconditions=(
            "flag truthy AND agent_runner supplied — enabling without a runner is a "
            "configuration error, never a silent fallback to the default executor",
        ),
        refusal_while_off="resolve_node_executor(agent_runner=None) raises V3AdapterDisabled",
        refusal_proof="pytest.raises(V3AdapterDisabled) on resolve_node_executor(env=on)",
        proving_ticket="T-V4-015",
    ),
    FlagEntry(
        flag="connector.enabled (W6 schedules)",
        surface="W6 schedule connector (cron/interval/one_shot)",
        default_state="off",
        enable_preconditions=(
            "enable(store=..., runner=...) with BOTH injected — half-armed refuses",
            "specs validated (workspace scope, kind shape, credential-by-reference)",
        ),
        refusal_while_off="ConnectorDisabledError on tick (no timer fires)",
        refusal_proof="ScheduleConnector().tick(now) raises ConnectorDisabledError; fired == 0",
        proving_ticket="T-V4-024",
    ),
    FlagEntry(
        flag="connector.enabled (W6 triggers)",
        surface="W6 trigger connector (webhook/event/manual)",
        default_state="off",
        enable_preconditions=(
            "enable(store=..., runner=...) with BOTH injected — half-armed refuses",
            "specs validated (workspace scope, endpoint-is-a-path, credential-by-reference)",
        ),
        refusal_while_off="ConnectorDisabledError on receive (no webhook listens, no event consumed)",  # noqa: E501
        refusal_proof="TriggerConnector().receive(address, payload, workspace_id=...) raises "
        "ConnectorDisabledError; consumed == 0",
        proving_ticket="T-V4-024",
    ),
    FlagEntry(
        flag="V3 executor seam (AGENT routing)",
        surface="executor AGENT-node seam",
        default_state="off",
        enable_preconditions=(
            "configure_agent_executor(NodeExecutor) — a Protocol-satisfying seam",
            "unset (default) keeps AGENT nodes on the injected executor",
        ),
        refusal_while_off="AGENT nodes fail honestly on the primary executor "
        "(NotImplementedError -> FAILED), never a mock success",
        refusal_proof="default executor + AGENT node -> run FAILED naming the node; "
        "spy-verified delegation when a seam is wired",
        proving_ticket="T-V4-011",
    ),
)


# --------------------------------------------------------------------------- #
# 3. ROLLBACK NOTE — ordered steps per surface, each with its proving ticket
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RollbackStep:
    """One ordered rollback step for one surface."""

    order: int
    surface: str
    action: str
    proving_ticket: str
    rerun: str


ROLLBACK_NOTE: tuple[RollbackStep, ...] = (
    # --- flag-off first: the cheapest, proven-inert rollback ---------------
    RollbackStep(
        order=1,
        surface="W3 LangGraph adapter (AGENT nodes)",
        action="Unset WORKFLOW_V3_ADAPTER (or set it falsy) and restart the worker; "
        "resolve_node_executor returns the default executor and the flag-off "
        "surface refuses every node with V3AdapterDisabled",
        proving_ticket="T-V4-015",
        rerun="python -m pytest tests/unit/test_workflow_agent_seam.py "
        "-k flag_off_parity_run",
    ),
    RollbackStep(
        order=2,
        surface="W6 schedule/trigger connectors",
        action="Call disable() on the connector (no store/runner needed to disable); "
        "tick/receive refuse with ConnectorDisabledError — no timer, no listener, "
        "no consumer",
        proving_ticket="T-V4-024",
        rerun="python -m pytest tests/unit/test_workflow_connectors.py "
        "-k DisabledByDefault",
    ),
    # --- state rollback: checkpoints and the migration down-file -----------
    RollbackStep(
        order=3,
        surface="workflow checkpoints (durable runs)",
        action="Runs already checkpointed resume from the newest checkpoint: settled "
        "nodes replay (never re-execute), unsettled nodes continue — no state is "
        "lost and none is re-applied",
        proving_ticket="T-V4-013",
        rerun="python -m pytest tests/unit/test_workflow_idempotency.py "
        "-k test_resume_replays_settled_and_continues_exactly",
    ),
    RollbackStep(
        order=4,
        surface="schema (workflow draft/revision migration)",
        action="Run the migration down-file against a scratch database; the "
        "down-file restores the exact prior schema (idempotent, repeatable). "
        "NEVER against the live database — the coordinator applies migrations",
        proving_ticket="T-V4-018",
        rerun="python -m pytest tests/unit/test_w7_parity_off_rollback.py "
        "-k probe3_migration_down",
    ),
    # --- envelope rollback: bounds are code constants -------------==========
    RollbackStep(
        order=5,
        surface="resource envelope (bounds)",
        action="Bounds are module constants with a drift test: reverting a bound "
        "means changing the constant AND this pack together (else the test "
        "fails). Rollback = restore the constant to the pack's default",
        proving_ticket="T-V4-006 / T-V4-010 / T-V4-012",
        rerun="python -m pytest tests/unit/test_workflow_release_pack.py "
        "-k envelope_matches_live_constants",
    ),
    RollbackStep(
        order=6,
        surface="fault behaviour (release evidence)",
        action="The fault matrix re-proves honesty under faults after any rollback: "
        "node crash, timeout, cancel-during-retry, double-dispatch, store failure "
        "and clock skew each end in their honest terminal state",
        proving_ticket="T-V4-025",
        rerun="python -m pytest tests/unit/test_workflow_fault_matrix.py",
    ),
)
