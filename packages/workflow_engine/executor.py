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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

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


@dataclass(frozen=True)
class _NodeOutcome:
    """Result of one bounded node execution (success or named failure)."""

    ok: bool
    result: Any = None
    error: str | None = None
    attempts: int = 0
    latency_ms: float = 0.0


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

    def _executor_for(self, node: NodeSpec) -> NodeExecutor:
        """Routing rule (stated): AGENT nodes go to the injected seam when one
        is wired; every other node (and AGENT with no seam wired) goes to the
        injected executor.  There is no third path."""
        if node.type is NodeType.AGENT and self._agent_executor is not None:
            return self._agent_executor
        return self._executor

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
    ) -> WorkflowRun:
        """Execute a compiled plan and return a ``WorkflowRun``."""
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

        run_id = f"wfrun-{uuid.uuid4().hex[:8]}"
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
    ) -> WorkflowRun:
        """The readiness loop (extracted so the caller can settle on cancel)."""
        while ready:
            # T-V4-009: cancellation is observed before every dispatch — a
            # cancelled run never proceeds to COMPLETED.
            if self._cancel.is_set():
                return self._finish_cancelled(run, nodes_by_id)

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

            outcome = await self._execute_within_bounds(
                node, node_input, revision.spec.workspace_id
            )
            node_run.latency_ms = outcome.latency_ms
            node_run.attempts = outcome.attempts
            if not outcome.ok:
                # T-V4-009 terminal honesty + T-V4-010 named bound breach:
                # the node failed (timeout / output cap / retries exhausted /
                # error payload) — the run fails naming it.
                node_run.status = NodeRunStatus.FAILED
                node_run.error = outcome.error
                run.node_runs[node_id] = node_run
                run.status = WorkflowRunStatus.FAILED
                run.error = outcome.error
                run.failed_node_id = node_id
                run.finished_at = datetime.now(UTC).isoformat()
                self._settle_remaining(run, nodes_by_id, f"{_SKIP_UPSTREAM}: node '{node_id}' failed")  # noqa: E501
                return run
            result = outcome.result
            node_run.status = NodeRunStatus.COMPLETED
            node_run.output_data = result
            run.node_runs[node_id] = node_run
            node_outputs[node_id] = result

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
                if isinstance(result, dict) and "output" in result:
                    run.final_output = result["output"]
                else:
                    run.final_output = result

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
            return run

        return self._finalize(run, plan, nodes_by_id, incoming_count)

    # ------------------------------------------------------------------ #
    # T-V4-010: bounded node execution (timeout, output cap, retry cap)
    # ------------------------------------------------------------------ #
    async def _execute_within_bounds(
        self,
        node: NodeSpec,
        node_input: dict[str, Any] | Any,
        workspace_id: str,
    ) -> _NodeOutcome:
        """Execute *node* inside all three bounds.

        * timeout — ``asyncio.wait_for`` per attempt; a hung node fails the
          node, never the run's liveness;
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
        attempts = 0
        last_error: str | None = None
        # T-V4-011: this node's executor — the seam for AGENT nodes when wired.
        executor = self._executor_for(node)
        while attempts < self._max_attempts:
            attempts += 1
            attempt_started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    executor.execute_node(node, node_input, workspace_id),
                    timeout=self._node_timeout_s,
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
                    f"node '{node.id}' exceeded the per-node timeout "
                    f"({self._node_timeout_s}s bound; attempt {attempts} "
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
                f"(bounds: timeout {self._node_timeout_s}s, max attempts "
                f"{self._max_attempts}): {last_error}"
            ),
            attempts=attempts,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

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
