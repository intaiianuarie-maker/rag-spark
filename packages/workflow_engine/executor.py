"""Workflow execution -- adapter Protocol + fake runtime for W0/W1.

The ``NodeExecutor`` Protocol is defined in ``interfaces.py``.  This module
provides:
- ``FakeNodeExecutor``: deterministic fake for W0/W1 tests (no real models/tools).
- ``WorkflowExecutor``: event-driven execution of an ``ExecutionPlan`` using
  a ``NodeExecutor``, producing a ``WorkflowRun`` with full node traces.

Production integration with a real LangGraph runtime (v3) will supply a
concrete ``NodeExecutor`` implementation in a later, reviewed change.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
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
    skipped) are marked ``SKIPPED``.  Failure honesty: the executor never
    marks a skipped node as COMPLETED.
    """

    
    def __init__(self, node_executor: NodeExecutor) -> None:
        self._executor = node_executor

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

        while ready:
            node_id = ready.popleft()
            node = nodes_by_id.get(node_id)
            if node is None:
                continue
            node_step: ExecutionStep | None = steps_by_node.get(node_id)

            # Check join readiness.
            if node_id != plan.trigger_node_id and node_step is not None:
                join_mode = node_step.join_mode
                if join_mode == "all" and active_count[node_id] < incoming_count[node_id]:
                    _mark_skipped(node_id, run)
                    continue
                if join_mode == "any" and active_count[node_id] == 0:
                    _mark_skipped(node_id, run)
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

            # Handle missing input.
            if node_input is None and node_id != plan.trigger_node_id:
                _mark_skipped(node_id, run)
                continue

            node_run = NodeRun(
                node_id=node_id,
                status=NodeRunStatus.RUNNING,
            )
            run.node_runs[node_id] = node_run

            t_start = time.perf_counter()
            try:
                result = await self._executor.execute_node(
                    node, node_input, revision.spec.workspace_id
                )
                node_run.status = NodeRunStatus.COMPLETED
                node_run.output_data = result
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

            except NotImplementedError as exc:
                node_run.status = NodeRunStatus.FAILED
                node_run.error = str(exc)
                run.node_runs[node_id] = node_run
                run.status = WorkflowRunStatus.FAILED
                run.finished_at = datetime.now(UTC).isoformat()
                return run
            except Exception as exc:
                node_run.status = NodeRunStatus.FAILED
                node_run.error = str(exc)
                run.node_runs[node_id] = node_run
                run.status = WorkflowRunStatus.FAILED
                run.finished_at = datetime.now(UTC).isoformat()
                return run
            finally:
                node_run.latency_ms = (time.perf_counter() - t_start) * 1000.0
                run.node_runs[node_id] = node_run

            if node.type == NodeType.OUTPUT:
                if isinstance(result, dict) and "output" in result:
                    run.final_output = result["output"]
                else:
                    run.final_output = result

                # Mark all unprocessed nodes as skipped.
        for node_id in nodes_by_id:
            if node_id not in run.node_runs:
                _mark_skipped(node_id, run)

        # Check overall run status: failed if any node failed.
        failed = any(
            nr.status == NodeRunStatus.FAILED for nr in run.node_runs.values()
        )
        if failed:
            run.status = WorkflowRunStatus.FAILED
            run.finished_at = datetime.now(UTC).isoformat()
            return run

        run.status = WorkflowRunStatus.COMPLETED
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


def _mark_skipped(node_id: str, run: WorkflowRun) -> None:
    """Mark a node as skipped in the run."""
    run.node_runs[node_id] = NodeRun(
        node_id=node_id,
        status=NodeRunStatus.SKIPPED,
    )
