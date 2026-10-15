"""Workflow execution -- adapter Protocol + fake runtime for W0/W1.

The ``NodeExecutor`` Protocol is defined in ``interfaces.py``.  This module
provides:
- ``FakeNodeExecutor``: deterministic fake for W0/W1 tests (no real models/tools).
- ``WorkflowExecutor``: event-driven execution of an ``ExecutionPlan`` using
  a ``NodeExecutor``, producing a ``WorkflowRun`` with full node traces.

Terminal-state honesty (T-V4-009): a run ends in exactly one of
``COMPLETED`` / ``FAILED`` / ``CANCELLED`` (see ``WorkflowRunStatus``):

* a node that raises, or returns an ``{"error": ...}`` payload, FAILS the
  run and the run names the node;
* a declared join barrier left incomplete (an ``all`` join with a skipped
  branch, an ``any`` join with no active input) is NOT a silent success —
  the node is skipped with that reason recorded and the run FAILS naming
  it;
* cancellation (``request_cancel`` mid-run, or an external
  ``asyncio.CancelledError``) settles the in-flight node, skips the rest
  and ends ``CANCELLED`` — never ``COMPLETED``;
* ``COMPLETED`` requires every node terminal with its output recorded;
  routing skips (a conditional branch not taken on a single-inbound node)
  stay disclosed SKIPPED and may still complete.

Production integration with a real LangGraph runtime (v3) will supply a
concrete ``NodeExecutor`` implementation in a later, reviewed change.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from .checkpoint import (
    CheckpointStore,
    ResumableExecutor,
    ResumeStateError,
    WorkflowCheckpoint,
    checkpoint_boundary,
)
from .interfaces import (
    ExecutionEdgeInfo,
    ExecutionPlan,
    ExecutionStep,
    NodeExecutor,
    NodeRun,
    NodeRunStatus,
    NodeSpec,
    NodeType,
    WorkflowRevision,
    WorkflowRun,
    WorkflowRunStatus,
)

#: Payload key an executor uses to return a structured node failure.  A
#: returned error is a failure — never absorbed into the run's outputs.
_ERROR_PAYLOAD_KEY = "error"

#: Skip reasons (recorded on ``NodeRun.error`` — a skip is disclosed).
_SKIP_BARRIER = "barrier-incomplete"
_SKIP_NO_INPUT = "no-active-inbound-edge"
_SKIP_UPSTREAM = "upstream-skipped"
_SKIP_CANCELLED = "cancelled"

# ---------------------------------------------------------------------------
# T-V4-013: idempotency under redelivery — the rule, stated.
#
# An idempotency KEY is ``(run_id, node_id)``; the FINGERPRINT is the
# canonical form of the work the key stands for (node id, type, config,
# input).  Executing a node consults the ledger FIRST:
#
# * key unseen                      → execute, then record (output once);
# * key seen, SAME fingerprint      → replay the recorded output without
#                                     executing (effect once, recorded once);
# * key seen, DIFFERENT fingerprint → :class:`IdempotencyKeyConflict`, loudly
#                                     (a key reused for different work is a
#                                     delivery error, never a silent hit).
#
# The same ledger serves retries, resumes and duplicate dispatches, because
# all three reduce to "execute this key again".
# ---------------------------------------------------------------------------


class IdempotencyKeyConflict(RuntimeError):
    """The same idempotency key was presented for different work."""


def _fingerprint(node: NodeSpec, node_input: dict[str, Any] | Any) -> str:
    """Canonical fingerprint of one node's work (id, type, config, input)."""
    return json.dumps(
        {
            "node_id": node.id,
            "type": str(node.type),
            "config": node.config,
            "input": node_input,
        },
        sort_keys=True,
        default=str,
    )


class IdempotencyLedger:
    """Records executed keys → outputs; replays on same-fingerprint hits."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], tuple[str, Any]] = {}

    def lookup(self, key: tuple[str, str], fingerprint: str) -> tuple[bool, Any]:
        """Return ``(hit, output)``; raise on a fingerprint mismatch."""
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        recorded_fp, output = entry
        if recorded_fp != fingerprint:
            raise IdempotencyKeyConflict(
                f"idempotency key {key} was recorded for different work "
                f"(fingerprint mismatch); refusing to replay or re-execute"
            )
        return True, output

    def record(self, key: tuple[str, str], fingerprint: str, output: Any) -> None:
        self._entries[key] = (fingerprint, output)

    def keys(self) -> set[tuple[str, str]]:
        return set(self._entries)

# ---------------------------------------------------------------------------
# T-V4-010: per-node bounds — stated, with defaults and reasons.
#
# Every node executes inside all three bounds; a breach fails THE NODE (the
# run then fails per the T-V4-009 terminal table) with a message naming the
# node, the bound and the observed value.  The bounds are executor/deployment
# level, never node-configurable: ``NodeSpec.config`` is untrusted imported
# data, so a node must not be able to widen or shrink its own bounds.
# ---------------------------------------------------------------------------

#: Per-node wall-clock bound.  A hung node (an infinite loop, a deadlock, a
#: never-answering tool) fails the node at this limit instead of hanging the
#: run's liveness.  30 s is generous for a single node step and keeps a
#: bounded run bounded: worst-case node cost is attempts x timeout.
NODE_TIMEOUT_S = 30.0

#: Per-node output cap (bytes of canonical JSON).  A flooding node (a full
#: corpus dump, an unbounded generation) is refused before its output is
#: stored in ``node_outputs``, fed downstream, or hashed — 1 MB is far above
#: any legitimate node payload (a document chunk is KBs) and far below the
#: OOM territory a hostile node would aim for.
MAX_NODE_OUTPUT_BYTES = 1_048_576

#: Retry cap per node (total attempts, including the first).  A transient
#: failure is retried; a permanently failing node fails loudly after this
#: many attempts — never an unbounded retry loop.
NODE_MAX_ATTEMPTS = 3

#: Retry backoff base (seconds); attempt n sleeps base * 2^(n-1), so the
#: schedule is deterministic and the total retry delay is bounded by
#: base * (2^attempts - 1) -- 0.15 s worst case at the defaults.
NODE_RETRY_BASE_S = 0.05

# ---------------------------------------------------------------------------
# T-V4-012: RUN-level envelope — stated, with defaults and reasons.
#
# The per-node bounds above are the inner layer (one node); these are the
# outer envelope (one run).  COMPOSITION RULE: the deadline is measured as
# WALL-CLOCK ELAPSED and the resource budget as NODE-SECONDS SPENT — so a
# node timeout (charged once, as its own timeout) never consumes the run
# deadline twice, and the effective per-node timeout is shortened to the
# run deadline's remaining time so a node cannot outlive the run.
# ---------------------------------------------------------------------------

#: Run-wide wall-clock deadline.  Expiry cancels the run cleanly (CANCELLED,
#: deadline named) instead of hanging it.  300 s covers any realistic
#: authored workflow — with the validator's 200-node cap a legitimate run
#: finishes far inside it.
RUN_DEADLINE_S = 300.0

#: Run-wide node-seconds budget: the sum of per-node execution time.  A run
#: that burns its node-seconds fails loudly naming the spent value — never
#: silent truncation.  600 s is 2x the deadline so parallelizable work
#: still fits while bounding total compute.
RUN_MAX_NODE_SECONDS = 600.0

#: Run-wide executed-node cap.  With the validator's 200-node graph cap this
#: is the accounting-side assertion that the executed set never exceeds the
#: declared graph (non-binding today; stated so a future executor that
#: re-enters nodes cannot escape accounting).
RUN_MAX_NODES = 200


@dataclass
class _RunEnvelope:
    """Run-level budget accounting (T-V4-012).

    ``clock`` is injectable for deterministic tests; elapsed is measured
    once per node boundary, so accounting is O(1) per node.
    """

    deadline_s: float | None = RUN_DEADLINE_S
    max_node_seconds: float | None = RUN_MAX_NODE_SECONDS
    max_nodes: int | None = RUN_MAX_NODES
    clock: Callable[[], float] = time.monotonic
    _started: float = field(default=0.0, init=False)
    node_seconds: float = field(default=0.0, init=False)
    nodes_executed: int = field(default=0, init=False)

    def start(self) -> None:
        self._started = self.clock()

    def elapsed(self) -> float:
        return self.clock() - self._started

    def remaining(self) -> float | None:
        if self.deadline_s is None:
            return None
        return self.deadline_s - self.elapsed()

    def deadline_exceeded(self) -> bool:
        return self.deadline_s is not None and self.elapsed() > self.deadline_s

    def node_seconds_exceeded(self) -> bool:
        return self.max_node_seconds is not None and self.node_seconds > self.max_node_seconds

    def nodes_exceeded(self) -> bool:
        return self.max_nodes is not None and self.nodes_executed > self.max_nodes

    def charge_node(self, latency_ms: float) -> None:
        """Account one executed node (its attempts + bounded backoff)."""
        self.nodes_executed += 1
        self.node_seconds += latency_ms / 1000.0


@dataclass(frozen=True)
class _NodeOutcome:
    """Result of one bounded node execution (success or named failure)."""

    ok: bool
    result: Any = None
    error: str | None = None
    attempts: int = 0
    latency_ms: float = 0.0
    #: True when the output was replayed from the idempotency ledger or a
    #: checkpoint rather than executed (T-V3-013 duplicate delivery).
    replayed: bool = False


def _output_bytes(result: Any) -> int:
    """Canonical output size in bytes — the measurement the cap is stated in.

    Strings are measured on their UTF-8 bytes; anything else serialises the
    same way the plan/hash paths do.  Never raises: a value that cannot be
    serialised falls back to its ``str()`` size (still a bound).
    """
    if isinstance(result, str):
        return len(result.encode("utf-8"))
    try:
        return len(json.dumps(result, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(result).encode("utf-8"))


class FakeNodeExecutor:
    """Deterministic fake ``NodeExecutor`` for W0/W1 testing.

    Implements the ``NodeExecutor`` Protocol without importing or calling
    LangGraph, Langflow, or real LLM/model tools.  Each node type produces
    a deterministic, schema-conformant output based on its config.

    Failure honesty: if a node type or required capability is not implemented
    in the fake, ``NotImplementedError`` is raised rather than silently
    producing mock success output.
    """

    # Capabilities the fake "supports" (all empty -- no real capabilities).
    _IMPLEMENTED_CAPABILITIES: ClassVar[set[str]] = set()

    @staticmethod
    def _has_capability(node: NodeSpec) -> bool:
        return all(
            cap in FakeNodeExecutor._IMPLEMENTED_CAPABILITIES
            for cap in node.required_capabilities
        )

    @staticmethod
    def _extract_primary(input_data: dict[str, Any] | Any) -> Any:
        """Extract the primary value from a named-port input dict.

        The Protocol always delivers ``dict[str, Any]`` (port_name -> value).
        For nodes with a single input port, we return the scalar value.
        For nodes with multiple ports, we return the dict as-is so the
        adapter can access individual ports by name.
        """
        if not isinstance(input_data, dict):
            return input_data
        if "input" in input_data and len(input_data) == 1:
            return input_data["input"]
        return input_data

    async def execute_node(
        self,
        node: NodeSpec,
        input_data: dict[str, Any] | Any,
        workspace_id: str,
    ) -> dict[str, Any]:
        """Execute *node* deterministically.  Never mocks success."""
        if not self._has_capability(node):
            if node.required_capabilities:
                raise NotImplementedError(
                    f"Node '{node.id}' requires capabilities "
                    f"{node.required_capabilities} not available in fake runtime"
                )

        ntype = node.type

        if ntype == NodeType.TRIGGER:
            return {"output": input_data}

        if ntype == NodeType.TRANSFORM:
            template = node.config.get("template")
            val = self._extract_primary(input_data)
            if template and isinstance(val, dict):
                try:
                    return {"output": template.format(**val)}
                except (KeyError, ValueError):
                    return {"output": template}
            if isinstance(val, dict) and "key" in node.config:
                return {"output": val.get(node.config["key"])}
            return {"output": val}

        if ntype == NodeType.CONDITION:
            field = node.config.get("field")
            expected = node.config.get("expected")
            val = self._extract_primary(input_data)
            if isinstance(val, dict):
                check_val = val.get(field) if field else val
            else:
                check_val = val
            if expected is not None:
                result = check_val == expected
            else:
                result = bool(check_val)
            # Pass through original data on "output" port for downstream
            # nodes, separate from the boolean "result" used for routing.
            return {"result": result, "output": val}

        if ntype == NodeType.OUTPUT:
            val = self._extract_primary(input_data)
            return {"output": val}

        if ntype == NodeType.RETRIEVER:
            chunks = node.config.get("chunk_ids", [])
            return {"output": list(chunks)}

        if ntype == NodeType.LLM:
            prompt = node.config.get("system_prompt", "")
            val = self._extract_primary(input_data)
            if isinstance(val, dict) and "text" in val:
                text = str(val["text"])
            else:
                text = str(val)
            return {"output": f"[FAKE:{prompt}:{text}]"}

        if ntype == NodeType.AGENT:
            raise NotImplementedError(
                f"Agent node '{node.id}' not implemented in fake runtime"
            )

        if ntype == NodeType.TOOL:
            raise NotImplementedError(
                f"Tool node '{node.id}' not implemented in fake runtime"
            )

        if ntype == NodeType.HTTP:
            raise NotImplementedError(
                f"HTTP node '{node.id}' not implemented in fake runtime"
            )

        raise NotImplementedError(
            f"Node type '{ntype}' not implemented in fake runtime"
        )


class WorkflowExecutor:
    """Event-driven executor for compiled ``ExecutionPlan``s.

    The executor processes nodes in topological readiness order.  When a
    node completes, its outgoing edges are evaluated:

    - Unconditional edges (no ``condition_value``) always activate the
      target node.
    - Conditional edges (from ``CONDITION`` nodes) are only activated if
      the condition node's ``"result"`` output matches the edge's
      ``condition_value``.

    Nodes that are not activated (because their conditional branch was
    skipped) are marked ``SKIPPED`` with the reason recorded.  Failure
    honesty: the executor never marks a skipped node as COMPLETED, and a
    run never reports COMPLETED while a failed node, an incomplete declared
    barrier, or a cancellation is inside it (T-V4-009).
    """

    def __init__(self, node_executor: NodeExecutor) -> None:
        self._executor = node_executor
        self._cancel = asyncio.Event()
        # T-V4-011: the AGENT-node seam.  ``None`` (the default) means AGENT
        # nodes route to the injected ``node_executor`` exactly as before —
        # flag-off is byte-identical by construction.  Production wiring
        # injects the W3 ``V3NodeExecutor`` (from
        # ``workflow_engine.adapters.v3_adapter``); the executor never imports
        # the adapter or the V3 product itself — seam-only, no direct v3
        # imports (adapters import the executor, never the reverse).
        self._agent_executor: NodeExecutor | None = None
        # T-V4-010: deployment-level bounds (see the constants above).  They
        # are constructor knobs, never per-node config — a node's own config
        # is untrusted imported data and must not widen its own bounds.
        self._node_timeout_s = NODE_TIMEOUT_S
        self._max_node_output_bytes = MAX_NODE_OUTPUT_BYTES
        self._max_attempts = NODE_MAX_ATTEMPTS
        self._retry_base_s = NODE_RETRY_BASE_S
        self._sleep = asyncio.sleep
        # T-V4-012: run-level envelope defaults (see ``_RunEnvelope``).
        self._run_deadline_s: float | None = RUN_DEADLINE_S
        self._run_max_node_seconds: float | None = RUN_MAX_NODE_SECONDS
        self._run_max_nodes: int | None = RUN_MAX_NODES
        self._clock: Callable[[], float] = time.monotonic
        #: Accounting of the most recent run (observability; asserted in tests).
        self.last_envelope: _RunEnvelope | None = None
        # T-V4-013: idempotency ledger + checkpoint seam (both injectable).
        self._ledger = IdempotencyLedger()
        self._checkpoint_store: CheckpointStore | None = None
        #: Checkpoint writes that failed (observability; a failed checkpoint
        #: must not kill the run, but it must not be silent either).
        self.checkpoint_failures: int = 0

    # ------------------------------------------------------------------ #
    # T-V4-013: idempotency + checkpoint seam configuration
    # ------------------------------------------------------------------ #
    @staticmethod
    def _seed_ledger_from_checkpoint(
        run_id: str,
        revision: WorkflowRevision,
        plan: ExecutionPlan,
        checkpoint: WorkflowCheckpoint,
        initial_input: dict[str, Any],
        ledger: IdempotencyLedger,
    ) -> None:
        """Record the settled nodes' work into *ledger* from the checkpoint.

        Each settled node's input is reconstructed exactly as the pre-kill
        run computed it (the trigger from ``initial_input``; every other node
        from the checkpointed predecessor outputs via the plan's
        ``input_mapping``), so the fingerprint is the one a redelivery of
        that same work will present.
        """
        nodes_by_id = {n.id: n for n in revision.spec.nodes}
        steps_by_node = {s.node_id: s for s in plan.steps}
        outputs = dict(checkpoint.node_outputs)
        for nid in checkpoint.completed_nodes:
            node = nodes_by_id.get(nid)
            if node is None or nid not in checkpoint.node_outputs:
                continue
            step = steps_by_node.get(nid)
            if node.type is NodeType.TRIGGER or step is None:
                node_input: Any = initial_input
            else:
                node_input = WorkflowExecutor._build_input(step, outputs)
            ledger.record(
                (run_id, nid),
                _fingerprint(node, node_input),
                checkpoint.node_outputs[nid],
            )
    def configure_idempotency_ledger(self, ledger: IdempotencyLedger | None) -> None:
        """Install the dedup ledger (``None`` installs a fresh in-memory one)."""
        self._ledger = ledger if ledger is not None else IdempotencyLedger()

    def configure_checkpoint_store(self, store: CheckpointStore | None) -> None:
        """Install the checkpoint seam used for cancel/resume durability."""
        self._checkpoint_store = store

    # ------------------------------------------------------------------ #
    # T-V4-013: checkpoint save/load across the seam
    # ------------------------------------------------------------------ #
    async def _load_checkpoint(self, run_id: str) -> WorkflowCheckpoint | None:
        """Newest checkpoint for ``run_id`` (``None`` when none is stored)."""
        if self._checkpoint_store is None:
            return None
        return await self._checkpoint_store.latest(run_id)

    async def _save_checkpoint(
        self,
        run_id: str,
        revision: WorkflowRevision,
        plan: ExecutionPlan,
        initial_input: dict[str, Any],
        run: WorkflowRun,
        *,
        run_complete: bool = False,
        run_failed: bool = False,
        checkpoint_seed: WorkflowCheckpoint | None,
    ) -> None:
        """Persist the run's settled set (T-V4-013).

        Saved after every settled node, so a cancel/envelope/terminal
        settlement resumes from exactly the last settled node.  Best-effort:
        a failed checkpoint write is counted (``checkpoint_failures``), never
        silently ignored and never fatal to the run — but it means a later
        resume starts from the last checkpoint that did land.
        """
        if self._checkpoint_store is None:
            return
        settled = [
            (node_id, record)
            for node_id, record in run.node_runs.items()
            if record.status == NodeRunStatus.COMPLETED
        ]
        if checkpoint_seed is not None:
            already = set(checkpoint_seed.completed_nodes)
            new_settled = [(nid, rec) for nid, rec in settled if nid not in already]
            if not new_settled and not run_complete and not run_failed:
                return  # nothing new since the resume point
        completed_nodes = [
            nid
            for nid, rec in settled
        ]
        node_outputs = {
            nid: rec.output_data for nid, rec in settled if rec.output_data is not None
        }
        last_node_id = completed_nodes[-1] if completed_nodes else None
        checkpoint = WorkflowCheckpoint(
            run_id=run_id,
            workflow_id=revision.spec.id,
            revision_id=plan.revision_hash,
            workspace_id=revision.spec.workspace_id,
            status=run.status.value,
            boundary=(
                checkpoint_boundary(
                    node_type=(
                        next(
                            (
                                n.type
                                for n in revision.spec.nodes
                                if n.id == last_node_id
                            ),
                            "",
                        )
                        if last_node_id
                        else ""
                    ),
                    run_complete=run_complete,
                    run_failed=run_failed,
                    previously_completed=checkpoint_seed is not None,
                )
                or "settled"
            ),
            initial_input=initial_input,
            completed_nodes=completed_nodes,
            node_outputs=node_outputs,
            node_runs={nid: rec for nid, rec in settled},
            revision_hash=plan.revision_hash,
            checkpointed_at=datetime.now(UTC).isoformat(),
        )
        try:
            await self._checkpoint_store.append(checkpoint)
        except Exception as exc:
            self.checkpoint_failures += 1
            import logging

            logging.getLogger("knowledge_os.workflow.executor").warning(
                "checkpoint_append_failed",
                extra={"run_id": run_id, "error_type": type(exc).__name__},
            )

    # ------------------------------------------------------------------ #
    # T-V4-012: run-level envelope configuration
    # ------------------------------------------------------------------ #
    def configure_run_bounds(
        self,
        *,
        deadline_s: float | None = None,
        max_node_seconds: float | None = None,
        max_nodes: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Set the run-level envelope before execution (T-V4-012).

        Values are validated at wiring time (deadline/node-seconds > 0,
        nodes >= 1) so a misconfiguration fails before the run starts.
        ``clock`` is the wall-clock source the deadline is measured against
        (injectable for deterministic tests).
        """
        if deadline_s is not None and deadline_s <= 0:
            raise ValueError("deadline_s must be > 0")
        if max_node_seconds is not None and max_node_seconds <= 0:
            raise ValueError("max_node_seconds must be > 0")
        if max_nodes is not None and max_nodes < 1:
            raise ValueError("max_nodes must be >= 1")
        if clock is not None:
            self._clock = clock
        self._run_deadline_s = deadline_s
        self._run_max_node_seconds = max_node_seconds
        self._run_max_nodes = max_nodes

    def _new_envelope(self) -> _RunEnvelope:
        envelope = _RunEnvelope(
            deadline_s=self._run_deadline_s,
            max_node_seconds=self._run_max_node_seconds,
            max_nodes=self._run_max_nodes,
            clock=self._clock,
        )
        self.last_envelope = envelope
        envelope.start()
        return envelope

    # ------------------------------------------------------------------ #
    # T-V4-011: AGENT routing (executor -> injected seam, never around it)
    # ------------------------------------------------------------------ #
    def configure_agent_executor(self, agent_executor: NodeExecutor | None) -> None:
        """Wire the executor AGENT nodes route through (T-V4-011).

        ``None`` restores the default routing (AGENT nodes go to the injected
        ``node_executor``).  The value must satisfy the ``NodeExecutor``
        Protocol — a misconfiguration fails at wiring time.
        """
        if agent_executor is not None and not isinstance(agent_executor, NodeExecutor):
            raise TypeError(
                "agent_executor must satisfy the NodeExecutor Protocol"
            )
        self._agent_executor = agent_executor

    @property
    def agent_executor(self) -> NodeExecutor | None:
        return self._agent_executor

    def _executor_for(
        self, node: NodeSpec, override: NodeExecutor | None = None
    ) -> NodeExecutor:
        """Routing rule (stated): AGENT nodes go to the injected seam when one
        is wired; every other node (and AGENT with no seam wired) goes to the
        run's primary executor (the injected one, or the resume replay
        wrapper when continuing from a checkpoint).  There is no third path."""
        if node.type is NodeType.AGENT and self._agent_executor is not None:
            return self._agent_executor
        return override if override is not None else self._executor

    def configure_bounds(
        self,
        *,
        node_timeout_s: float | None = None,
        max_node_output_bytes: int | None = None,
        max_attempts: int | None = None,
        retry_base_s: float | None = None,
        sleep: Any = None,
    ) -> None:
        """Narrow (or set) the bounds before execution; cannot widen past the
        stated defaults implicitly — a caller must pass a value explicitly.

        Values are validated: timeout/base must be > 0 and attempts >= 1,
        so a misconfiguration fails at wiring time rather than at run time.
        """
        if node_timeout_s is not None:
            if node_timeout_s <= 0:
                raise ValueError("node_timeout_s must be > 0")
            self._node_timeout_s = node_timeout_s
        if max_node_output_bytes is not None:
            if max_node_output_bytes < 1:
                raise ValueError("max_node_output_bytes must be >= 1")
            self._max_node_output_bytes = max_node_output_bytes
        if max_attempts is not None:
            if max_attempts < 1:
                raise ValueError("max_attempts must be >= 1")
            self._max_attempts = max_attempts
        if retry_base_s is not None:
            if retry_base_s < 0:
                raise ValueError("retry_base_s must be >= 0")
            self._retry_base_s = retry_base_s
        if sleep is not None:
            self._sleep = sleep

    def request_cancel(self) -> None:
        """Request cancellation; observed before the next node dispatch.

        Deterministic by design: a node already executing finishes and its
        output is recorded (settled), no further node starts, and the run
        ends ``CANCELLED`` — never ``COMPLETED``.
        """
        self._cancel.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    async def execute(
        self,
        plan: ExecutionPlan,
        revision: WorkflowRevision,
        initial_input: dict[str, Any],
        *,
        run_id: str | None = None,
        resume: bool = False,
    ) -> WorkflowRun:
        """Execute a compiled plan and return a ``WorkflowRun``.

        T-V4-013: ``run_id`` fixes the run's identity across deliveries and
        resumes (the idempotency key is ``(run_id, node_id)``); ``resume=True``
        continues from the newest checkpoint for that run — settled nodes are
        replayed from the checkpoint (never re-executed) and the unsettled
        remainder continues exactly.
        """
        # Verify plan matches revision: recompute hash from spec and compare
        # against the hash stored in the plan at compile time.  This catches
        # post-compile mutations to revision.spec (top-level or nested).
        computed = WorkflowRevision._compute_content_hash(revision.spec)
        if computed != plan.revision_hash:
            raise ValueError(
                f"Plan revision_hash ({plan.revision_hash}) does not match "
                f"revision content_hash ({computed}) — spec was modified "
                f"after compilation"
            )

        run_id = run_id or f"wfrun-{uuid.uuid4().hex[:8]}"
        # A fresh delivery starts a fresh ledger pass for this run id; resume
        # re-seeds it from the checkpoint below.
        self._cancel = asyncio.Event()
        checkpoint: WorkflowCheckpoint | None = None
        base_executor = self._executor
        if resume:
            checkpoint = await self._load_checkpoint(run_id)
            if checkpoint is None and self._checkpoint_store is not None:
                raise ResumeStateError(
                    f"resume requested but no checkpoint found for run {run_id!r}"
                )
            if checkpoint is not None:
                if checkpoint.workspace_id and checkpoint.workspace_id != revision.spec.workspace_id:  # noqa: E501
                    raise ResumeStateError(
                        "resume workspace mismatch: "
                        f"{revision.spec.workspace_id!r} != checkpointed "
                        f"{checkpoint.workspace_id!r}"
                    )
                if checkpoint.revision_hash and checkpoint.revision_hash != plan.revision_hash:
                    raise ResumeStateError(
                        "resume revision mismatch: the checkpoint was taken "
                        f"for revision {checkpoint.revision_hash}, this plan "
                        f"is {plan.revision_hash}"
                    )
                # The T-V4-016 wrapper replays checkpointed outputs and
                # delegates the rest — the core loop stays untouched.
                base_executor = ResumableExecutor(self._executor, checkpoint)
                # Re-seed the idempotency ledger from the checkpoint with the
                # work each settled node actually did (its input reconstructed
                # from the checkpointed outputs), so a redelivery AFTER the
                # resume still dedups — and a key reused for different work
                # still conflicts.
                self._seed_ledger_from_checkpoint(
                    run_id, revision, plan, checkpoint, initial_input, self._ledger
                )
        now = datetime.now(UTC).isoformat()

        run = WorkflowRun(
            id=run_id,
            workspace_id=revision.spec.workspace_id,
            workflow_id=revision.spec.id,
            status=WorkflowRunStatus.RUNNING,
            initial_input=initial_input,
            created_at=now,
        )

        nodes_by_id = {n.id: n for n in revision.spec.nodes}
        steps_by_node: dict[str, ExecutionStep] = {
            s.node_id: s for s in plan.steps
        }

        # Build edge maps.
        outgoing: dict[str, list[ExecutionEdgeInfo]] = {
            n.id: [] for n in revision.spec.nodes
        }
        incoming_count: dict[str, int] = {n.id: 0 for n in revision.spec.nodes}
        for step in plan.steps:
            for edge in step.outgoing_edges:
                if edge.target_node_id in outgoing:
                    outgoing[step.node_id].append(edge)
                    incoming_count[edge.target_node_id] += 1

        # Track state per node.
        pending: dict[str, int] = dict(incoming_count)
        active_count: dict[str, int] = {n.id: 0 for n in revision.spec.nodes}
        node_outputs: dict[str, dict[str, Any]] = {}

        # Ready queue: nodes with 0 pending incoming edges (triggers).
        ready: deque[str] = deque(
            sorted([nid for nid, cnt in pending.items() if cnt == 0])
        )

        envelope = self._new_envelope()
        try:
            return await self._run_loop(
                plan=plan,
                revision=revision,
                initial_input=initial_input,
                run=run,
                nodes_by_id=nodes_by_id,
                steps_by_node=steps_by_node,
                outgoing=outgoing,
                incoming_count=incoming_count,
                pending=pending,
                active_count=active_count,
                node_outputs=node_outputs,
                ready=ready,
                envelope=envelope,
                run_id=run_id,
                executor_override=base_executor,
                checkpoint_seed=checkpoint,
            )
        except asyncio.CancelledError:
            # External task cancellation: settle the in-flight node, end
            # CANCELLED, then re-raise (asyncio semantics preserved).
            for record in run.node_runs.values():
                if record.status == NodeRunStatus.RUNNING:
                    record.status = NodeRunStatus.FAILED
                    record.error = "cancelled mid-execution"
            for node_id in nodes_by_id:
                if node_id not in run.node_runs:
                    _mark_skipped(node_id, run, reason=f"{_SKIP_CANCELLED}: run cancelled")
            run.status = WorkflowRunStatus.CANCELLED
            run.error = "run cancelled (task cancelled)"
            run.finished_at = datetime.now(UTC).isoformat()
            raise

    async def _run_loop(
        self,
        *,
        plan: ExecutionPlan,
        revision: WorkflowRevision,
        initial_input: dict[str, Any],
        run: WorkflowRun,
        nodes_by_id: dict[str, NodeSpec],
        steps_by_node: dict[str, ExecutionStep],
        outgoing: dict[str, list[ExecutionEdgeInfo]],
        incoming_count: dict[str, int],
        pending: dict[str, int],
        active_count: dict[str, int],
        node_outputs: dict[str, dict[str, Any]],
        ready: deque[str],
        envelope: _RunEnvelope,
        run_id: str,
        executor_override: NodeExecutor,
        checkpoint_seed: WorkflowCheckpoint | None,
    ) -> WorkflowRun:
        """The readiness loop (extracted so the caller can settle on cancel).

        T-V4-013: ``run_id`` fixes the idempotency keys; ``executor_override``
        is this run's primary executor (replay wrapper when resuming);
        ``checkpoint_seed`` names the checkpoint a resume started from (None
        on a fresh delivery).
        """
        while ready:
            # T-V4-009: cancellation is observed before every dispatch — a
            # cancelled run never proceeds to COMPLETED.
            if self._cancel.is_set():
                # T-V4-013: persist the settled set BEFORE ending, so the
                # resume starts from the last settled node.
                await self._save_checkpoint(
                    run_id, revision, plan, initial_input, run,
                    run_failed=True, checkpoint_seed=checkpoint_seed,
                )
                return self._finish_cancelled(run, nodes_by_id)
            # T-V4-012: the run deadline is observed before every dispatch —
            # no node starts once the envelope's wall-clock is spent.
            if envelope.deadline_exceeded():
                await self._save_checkpoint(
                    run_id, revision, plan, initial_input, run,
                    run_failed=True, checkpoint_seed=checkpoint_seed,
                )
                return self._finish_deadline(run, nodes_by_id, envelope)

            node_id = ready.popleft()
            node = nodes_by_id.get(node_id)
            if node is None:
                continue
            node_step: ExecutionStep | None = steps_by_node.get(node_id)

            # Check join readiness.
            if node_id != plan.trigger_node_id and node_step is not None:
                join_mode = node_step.join_mode
                if join_mode in {"all", "any", "first"}:
                    active = active_count[node_id]
                    inbound = incoming_count[node_id]
                    if active == 0:
                        # Nothing arrived.  On a single wire this is routing
                        # (the branch was not taken); on a join node it is an
                        # incomplete declared barrier.
                        if inbound < 2:
                            reason = (
                                f"{_SKIP_NO_INPUT}: no active inbound edge "
                                f"delivered a value"
                            )
                        else:
                            reason = (
                                f"{_SKIP_BARRIER}: declared {join_mode}-barrier "
                                f"received no active input"
                            )
                        _mark_skipped(node_id, run, reason=reason)
                        WorkflowExecutor._propagate_skipped(
                            node_id, outgoing, pending, ready
                        )
                        continue
                    if join_mode == "all" and active < inbound:
                        _mark_skipped(
                            node_id, run,
                            reason=(
                                f"{_SKIP_BARRIER}: declared all-barrier received "
                                f"{active}/{inbound} active inputs"
                            ),
                        )
                        WorkflowExecutor._propagate_skipped(
                            node_id, outgoing, pending, ready
                        )
                        continue

                        # Build input data.
            if node_step and node_id == plan.trigger_node_id:
                node_input: dict[str, Any] | Any = dict(initial_input)
            elif node_step:
                node_input = WorkflowExecutor._build_input(
                    node_step,
                    node_outputs
                )
            else:
                node_input = None

            # Handle missing input (a single-inbound node whose branch was
            # not taken lands here or above — either way it is routing).
            if node_input is None and node_id != plan.trigger_node_id:
                _mark_skipped(
                    node_id, run,
                    reason=(
                        f"{_SKIP_NO_INPUT}: no active inbound edge delivered a value"
                    ),
                )
                WorkflowExecutor._propagate_skipped(
                    node_id, outgoing, pending, ready
                )
                continue

            node_run = NodeRun(
                node_id=node_id,
                status=NodeRunStatus.RUNNING,
            )
            run.node_runs[node_id] = node_run

            # T-V4-012 composition rule: the effective per-node timeout is
            # shortened to the run deadline's remaining time, so a node can
            # never outlive the run; the node's own bound (T-V4-010) stays the
            # inner layer and is named when it binds.
            remaining = envelope.remaining()
            if remaining is not None and remaining < self._node_timeout_s:
                effective_timeout: float | None = remaining
                timeout_reason = "run-deadline-adjusted per-node"
            else:
                effective_timeout = None
                timeout_reason = None

            outcome = await self._execute_within_bounds(
                node,
                node_input,
                revision.spec.workspace_id,
                run_id=run_id,
                executor_override=executor_override,
                timeout_s=effective_timeout,
                timeout_reason=timeout_reason,
            )
            node_run.latency_ms = outcome.latency_ms
            node_run.attempts = outcome.attempts
            # The node's own record is written first (honest per-node state),
            # then the run-level envelope decides the run's terminal status.
            if outcome.ok:
                node_run.status = NodeRunStatus.COMPLETED
                node_run.output_data = outcome.result
            else:
                node_run.status = NodeRunStatus.FAILED
                node_run.error = outcome.error
            run.node_runs[node_id] = node_run
            # Run-level accounting: one executed node, charged once (its
            # attempts + bounded backoff) — the deadline is NOT double-counted.
            envelope.charge_node(outcome.latency_ms)
            # The envelope outranks the node's own outcome: a run whose
            # wall-clock or budget is spent ends on the envelope, named.
            if envelope.deadline_exceeded():
                await self._save_checkpoint(
                    run_id, revision, plan, initial_input, run,
                    run_failed=True, checkpoint_seed=checkpoint_seed,
                )
                return self._finish_deadline(
                    run, nodes_by_id, envelope, last_node_id=node_id
                )
            if envelope.node_seconds_exceeded():
                run.status = WorkflowRunStatus.FAILED
                run.error = (
                    f"run node-seconds budget exhausted: "
                    f"{envelope.node_seconds:.3f}s spent, budget "
                    f"{envelope.max_node_seconds}s (node '{node_id}' crossed it)"
                )
                run.failed_node_id = node_id
                run.finished_at = datetime.now(UTC).isoformat()
                self._settle_remaining(run, nodes_by_id, f"{_SKIP_UPSTREAM}: node '{node_id}' failed")  # noqa: E501
                await self._save_checkpoint(
                    run_id, revision, plan, initial_input, run,
                    run_failed=True, checkpoint_seed=checkpoint_seed,
                )
                return run
            if envelope.nodes_exceeded():
                run.status = WorkflowRunStatus.FAILED
                run.error = (
                    f"run node-count budget exhausted: "
                    f"{envelope.nodes_executed} nodes executed, budget "
                    f"{envelope.max_nodes} (node '{node_id}' crossed it)"
                )
                run.failed_node_id = node_id
                run.finished_at = datetime.now(UTC).isoformat()
                self._settle_remaining(run, nodes_by_id, f"{_SKIP_UPSTREAM}: node '{node_id}' failed")  # noqa: E501
                await self._save_checkpoint(
                    run_id, revision, plan, initial_input, run,
                    run_failed=True, checkpoint_seed=checkpoint_seed,
                )
                return run
            if not outcome.ok:
                # T-V4-009 terminal honesty + T-V4-010 named bound breach:
                # the node failed (timeout / output cap / retries exhausted /
                # error payload) — the run fails naming it.
                run.status = WorkflowRunStatus.FAILED
                run.error = outcome.error
                run.failed_node_id = node_id
                run.finished_at = datetime.now(UTC).isoformat()
                self._settle_remaining(run, nodes_by_id, f"{_SKIP_UPSTREAM}: node '{node_id}' failed")  # noqa: E501
                await self._save_checkpoint(
                    run_id, revision, plan, initial_input, run,
                    run_failed=True, checkpoint_seed=checkpoint_seed,
                )
                return run
            # T-V4-013: the settled set grows — persist it so a cancel or a
            # kill resumes from exactly this node.
            await self._save_checkpoint(
                run_id, revision, plan, initial_input, run,
                checkpoint_seed=checkpoint_seed,
            )
            node_outputs[node_id] = outcome.result

            # Process outgoing edges.
            newly_ready: list[str] = []
            for edge in outgoing.get(node_id, []):
                is_active = WorkflowExecutor._edge_is_active(
                    edge, node, node_outputs
                )
                if is_active:
                    active_count[edge.target_node_id] += 1
                pending[edge.target_node_id] -= 1
                if pending[edge.target_node_id] == 0 and \
                   edge.target_node_id not in ready:
                    newly_ready.append(edge.target_node_id)

            for nid in sorted(newly_ready):
                ready.append(nid)

            if node.type == NodeType.OUTPUT:
                if isinstance(outcome.result, dict) and "output" in outcome.result:
                    run.final_output = outcome.result["output"]
                else:
                    run.final_output = outcome.result

        # Mark all unprocessed nodes as skipped (disclosed, with reason).
        # A join node that never got to evaluate its barrier is named as
        # such — the barrier was never satisfied, not merely unobserved.
        for node_id in nodes_by_id:
            if node_id in run.node_runs:
                continue
            if incoming_count.get(node_id, 0) >= 2:
                _mark_skipped(
                    node_id, run,
                    reason=(
                        f"{_SKIP_BARRIER}: declared barrier never satisfied "
                        f"({active_count[node_id]}/{incoming_count[node_id]} active inputs)"
                    ),
                )
            else:
                _mark_skipped(node_id, run, reason=f"{_SKIP_UPSTREAM}: never reached")

        # Any failed node fails the run and names the node.
        failed = [
            nr for nr in run.node_runs.values()
            if nr.status == NodeRunStatus.FAILED
        ]
        if failed:
            node_id = failed[0].node_id
            run.status = WorkflowRunStatus.FAILED
            run.error = f"node '{node_id}' failed: {failed[0].error}"
            run.failed_node_id = node_id
            run.finished_at = datetime.now(UTC).isoformat()
            await self._save_checkpoint(
                run_id, revision, plan, initial_input, run,
                run_failed=True, checkpoint_seed=checkpoint_seed,
            )
            return run

        finalized = self._finalize(run, plan, nodes_by_id, incoming_count)
        await self._save_checkpoint(
            run_id, revision, plan, initial_input, finalized,
            run_complete=finalized.status == WorkflowRunStatus.COMPLETED,
            run_failed=finalized.status == WorkflowRunStatus.FAILED,
            checkpoint_seed=checkpoint_seed,
        )
        return finalized

    # ------------------------------------------------------------------ #
    # T-V4-010: bounded node execution (timeout, output cap, retry cap)
    # ------------------------------------------------------------------ #
    async def _execute_within_bounds(
        self,
        node: NodeSpec,
        node_input: dict[str, Any] | Any,
        workspace_id: str,
        *,
        run_id: str = "",
        executor_override: NodeExecutor | None = None,
        timeout_s: float | None = None,
        timeout_reason: str | None = None,
    ) -> _NodeOutcome:
        """Execute *node* inside all three bounds.

        * timeout — ``asyncio.wait_for`` per attempt; a hung node fails the
          node, never the run's liveness.  ``timeout_s`` overrides the
          per-node bound for this node only (T-V4-012 passes the run
          deadline's remaining time so a node cannot outlive the run);
          ``timeout_reason`` names the adjusted bound in the breach message.
        * output cap — the result's canonical byte size is checked before
          it is stored or propagated;
        * retry cap — a transient failure retries with bounded exponential
          backoff up to ``max_attempts``; a permanent error
          (``NotImplementedError``) does not retry; ``CancelledError``
          propagates untouched (control flow, not a node failure).

        Every breach message names the node, the bound and the observed
        value; the outcome records the attempt count either way.
        """
        started = time.perf_counter()
        # T-V4-013: the idempotency ledger is consulted BEFORE any execution —
        # a redelivery of the same key replays the recorded output; a key
        # presented for different work raises (never silently re-executed).
        key = (run_id, node.id)
        fingerprint = _fingerprint(node, node_input)
        hit, recorded = self._ledger.lookup(key, fingerprint)
        if hit:
            return _NodeOutcome(
                ok=True, result=recorded, attempts=0, latency_ms=0.0, replayed=True
            )
        attempts = 0
        last_error: str | None = None
        # T-V4-011: this node's executor — the seam for AGENT nodes when wired.
        executor = self._executor_for(node, executor_override)
        effective_timeout = (
            self._node_timeout_s if timeout_s is None else timeout_s
        )
        timeout_label = timeout_reason or "per-node"
        while attempts < self._max_attempts:
            attempts += 1
            attempt_started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    executor.execute_node(node, node_input, workspace_id),
                    timeout=effective_timeout,
                )
            except asyncio.CancelledError:
                # Control flow: the caller settles the run (T-V4-009).
                raise
            except NotImplementedError as exc:
                # Permanent — retrying cannot help.
                return _NodeOutcome(
                    ok=False,
                    error=f"node '{node.id}' failed: {exc}",
                    attempts=attempts,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                )
            except TimeoutError:
                observed = time.perf_counter() - attempt_started
                last_error = (
                    f"node '{node.id}' exceeded the {timeout_label} timeout "
                    f"({effective_timeout}s bound; attempt {attempts} "
                    f"timed out after {observed:.3f}s)"
                )
            except Exception as exc:
                last_error = (
                    f"node '{node.id}' failed (attempt {attempts}): {exc}"
                )
            else:
                size = _output_bytes(result)
                if size > self._max_node_output_bytes:
                    return _NodeOutcome(
                        ok=False,
                        error=(
                            f"node '{node.id}' returned {size} bytes of "
                            f"output; the per-node cap is "
                            f"{self._max_node_output_bytes} bytes"
                        ),
                        attempts=attempts,
                        latency_ms=(time.perf_counter() - started) * 1000.0,
                    )
                # T-V4-009: a returned error payload is a failure — never
                # absorbed into the run's outputs.
                if isinstance(result, dict) and _ERROR_PAYLOAD_KEY in result:
                    return _NodeOutcome(
                        ok=False,
                        error=(
                            f"node '{node.id}' returned an error payload: "
                            f"{result[_ERROR_PAYLOAD_KEY]}"
                        ),
                        attempts=attempts,
                        latency_ms=(time.perf_counter() - started) * 1000.0,
                    )
                self._ledger.record(key, fingerprint, result)
                return _NodeOutcome(
                    ok=True,
                    result=result,
                    attempts=attempts,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                )

            # Bounded exponential backoff between attempts.
            if attempts < self._max_attempts:
                await self._sleep(self._retry_base_s * (2 ** (attempts - 1)))

        return _NodeOutcome(
            ok=False,
            error=(
                f"node '{node.id}' failed after {attempts} attempts "
                f"(bounds: timeout {effective_timeout}s, max attempts "
                f"{self._max_attempts}): {last_error}"
            ),
            attempts=attempts,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    # ------------------------------------------------------------------ #
    # T-V4-012: run-level envelope settlement
    # ------------------------------------------------------------------ #
    def _finish_deadline(
        self,
        run: WorkflowRun,
        nodes_by_id: dict[str, NodeSpec],
        envelope: _RunEnvelope,
        last_node_id: str | None = None,
    ) -> WorkflowRun:
        """Settle the run after the run-wide deadline expired.

        Completed nodes keep their recorded outputs (settled); nodes that
        never ran are SKIPPED with the cancellation reason; the run ends
        CANCELLED with the deadline and the observed elapsed time named —
        never COMPLETED, never hung.
        """
        for node_id in nodes_by_id:
            if node_id not in run.node_runs:
                _mark_skipped(
                    node_id, run,
                    reason=f"{_SKIP_CANCELLED}: run deadline reached before start",
                )
        run.status = WorkflowRunStatus.CANCELLED
        suffix = f" (node '{last_node_id}' crossed it)" if last_node_id else ""
        run.error = (
            f"run deadline reached: {envelope.elapsed():.3f}s elapsed, "
            f"deadline {envelope.deadline_s}s{suffix}"
        )
        run.failed_node_id = None
        run.finished_at = datetime.now(UTC).isoformat()
        return run

    # ------------------------------------------------------------------ #
    # T-V4-009: terminal-state finalization
    # ------------------------------------------------------------------ #
    @staticmethod
    def _propagate_skipped(
        node_id: str,
        outgoing: dict[str, list[ExecutionEdgeInfo]],
        pending: dict[str, int],
        ready: deque[str],
    ) -> None:
        """Settle a skipped node's outgoing edges as INACTIVE.

        Without this, a skipped branch abandons its edges: downstream
        pending counters never reach zero, join nodes are never dequeued,
        and the whole tail of the graph collapses into "never reached" —
        an ``any``/``first`` barrier could never observe the inputs it DID
        receive.  The edges are inactive (a skipped node produced nothing),
        but they still count toward readiness so the barrier is evaluated
        honestly.
        """
        newly_ready: list[str] = []
        for edge in outgoing.get(node_id, []):
            pending[edge.target_node_id] -= 1
            if pending[edge.target_node_id] == 0 and edge.target_node_id not in ready:
                newly_ready.append(edge.target_node_id)
        for nid in sorted(newly_ready):
            ready.append(nid)

    @staticmethod
    def _settle_remaining(
        run: WorkflowRun,
        nodes_by_id: dict[str, NodeSpec],
        reason: str,
    ) -> None:
        """Record every node that never ran as SKIPPED with *reason*.

        A terminal run has a record for EVERY node — a missing record is a
        silent hole, which is exactly what this closes (a failed run still
        discloses what did not run).
        """
        for node_id in nodes_by_id:
            if node_id not in run.node_runs:
                _mark_skipped(node_id, run, reason=reason)
    def _finalize(
        self,
        run: WorkflowRun,
        plan: ExecutionPlan,
        nodes_by_id: dict[str, NodeSpec],
        incoming_count: dict[str, int],
    ) -> WorkflowRun:
        """Decide COMPLETED vs FAILED from the honest per-node record.

        COMPLETED requires: every node in the plan has a terminal status,
        no node FAILED, no node skipped because a declared barrier was left
        incomplete (or sits downstream of one), and every non-skipped node
        recorded its output.
        """
        # A node in the plan with no record at all is a hole — fail closed.
        for node_id in nodes_by_id:
            if node_id not in run.node_runs:
                run.status = WorkflowRunStatus.FAILED
                run.error = f"node '{node_id}' has no execution record"
                run.failed_node_id = node_id
                run.finished_at = datetime.now(UTC).isoformat()
                return run

        # An incomplete DECLARED barrier is not a silent success: the run
        # names the first join node (two or more inbound edges) whose
        # barrier was left incomplete, or that never got the chance.  A
        # single-inbound node skipped because its branch was not taken is
        # routing, not a barrier failure — it stays disclosed SKIPPED.
        for node_id in nodes_by_id:
            if incoming_count.get(node_id, 0) < 2:
                continue
            record = run.node_runs[node_id]
            if record.status != NodeRunStatus.SKIPPED:
                continue
            reason = record.error or ""
            if reason.startswith(_SKIP_BARRIER) or reason.startswith(_SKIP_UPSTREAM):
                run.status = WorkflowRunStatus.FAILED
                run.error = (
                    f"declared barrier incomplete at node '{node_id}': {reason}"
                )
                run.failed_node_id = node_id
                run.finished_at = datetime.now(UTC).isoformat()
                return run

        # Every non-skipped node must have recorded its output.
        for node_id, record in run.node_runs.items():
            if record.status == NodeRunStatus.SKIPPED:
                continue
            if record.output_data is None:
                run.status = WorkflowRunStatus.FAILED
                run.error = (
                    f"node '{node_id}' is {record.status.value} with no output recorded"
                )
                run.failed_node_id = node_id
                run.finished_at = datetime.now(UTC).isoformat()
                return run

        run.status = WorkflowRunStatus.COMPLETED
        run.error = None
        run.failed_node_id = None
        run.finished_at = datetime.now(UTC).isoformat()
        return run

    def _finish_cancelled(
        self,
        run: WorkflowRun,
        nodes_by_id: dict[str, NodeSpec],
    ) -> WorkflowRun:
        """Settle the run after a cancellation request.

        Completed nodes keep their recorded outputs; nodes that never ran
        are SKIPPED with the cancellation reason; the run ends CANCELLED and
        never COMPLETED.
        """
        for node_id in nodes_by_id:
            if node_id not in run.node_runs:
                _mark_skipped(node_id, run, reason=f"{_SKIP_CANCELLED}: run cancelled before start")
        run.status = WorkflowRunStatus.CANCELLED
        run.error = "run cancelled by request"
        run.failed_node_id = None
        run.finished_at = datetime.now(UTC).isoformat()
        return run

    # ------------------------------------------------------------------ #
    @staticmethod
    def _edge_is_active(
        edge: ExecutionEdgeInfo,
        source_node: NodeSpec,
        node_outputs: dict[str, dict[str, Any]],
    ) -> bool:
        """Return True if a conditional edge is active given the source's output.

        For ``CONDITION`` nodes, the routing check compares against the
        ``"result"`` output port, separate from the ``source_port`` used
        for data routing.  This allows the condition to pass through the
        original input data on a different port (e.g. ``"output"``) while
        routing decisions use the boolean ``"result"``.
        """
        if edge.condition_value is None:
            return True  # unconditional edge

        output = node_outputs.get(source_node.id, {})

        if source_node.type == NodeType.CONDITION:
            port_value = output.get("result")
        else:
            port_value = output.get(edge.source_port)

        return port_value == edge.condition_value

    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_input(
        step: ExecutionStep,
        node_outputs: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | Any:
        """Build input data for a node from its completed predecessors' outputs."""
        if not step.input_mapping:
            return None

        port_values: dict[str, list[Any]] = {}
        for port_name, sources in step.input_mapping.items():
            values: list[Any] = []
            for src in sources:
                parts = src.split(":", 1)
                if len(parts) != 2:
                    continue
                src_node, src_port = parts
                if src_node in node_outputs and src_port in node_outputs[src_node]:
                    values.append(node_outputs[src_node][src_port])
            port_values[port_name] = values

        if len(port_values) == 0:
            return None
        elif len(port_values) == 1:
            vals = next(iter(port_values.values()))
            if len(vals) == 1:
                return vals[0]
            elif len(vals) > 1:
                return vals
            else:
                return None
        else:
            result: dict[str, Any] = {}
            for port_name, vals in port_values.items():
                if len(vals) == 1:
                    result[port_name] = vals[0]
                elif len(vals) > 1:
                    result[port_name] = vals
                else:
                    result[port_name] = None
            return result


def _mark_skipped(node_id: str, run: WorkflowRun, reason: str | None = None) -> None:
    """Mark a node as skipped in the run.

    ``reason`` (T-V4-009) is recorded on the run so a skip is disclosed:
    ``barrier-incomplete`` / ``no-active-inbound-edge`` / ``upstream-skipped``
    / ``cancelled``.
    """
    run.node_runs[node_id] = NodeRun(
        node_id=node_id,
        status=NodeRunStatus.SKIPPED,
        error=reason,
    )
