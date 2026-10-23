"""Workflow Engine execution engine (Section 30).

Executes DAG workflows with conditional branching, node execution tracing,
and error handling.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

from models.interfaces import ChatMessage, GenerationRequest, LLMProvider, MessageRole

from .interfaces import (
    NodeRun,
    NodeRunStatus,
    NodeType,
    Workflow,
    WorkflowNode,
    WorkflowRun,
    WorkflowRunStatus,
)


class WorkflowEngine:
    """DAG execution engine for automated knowledge workflows."""

    def __init__(self, llm_provider: LLMProvider | None = None) -> None:
        self.llm = llm_provider

    async def execute_node(
        self,
        node: WorkflowNode,
        input_data: Any,
        workspace_id: str,
    ) -> Any:
        """Execute a single workflow node based on its type."""
        ntype = node.type

        if ntype == NodeType.TRIGGER:
            return input_data

        if ntype == NodeType.TRANSFORM:
            # Simple dictionary / template transform
            template = node.config.get("template")
            if template and isinstance(input_data, dict):
                try:
                    return template.format(**input_data)
                except Exception:
                    return template
            if isinstance(input_data, dict) and "key" in node.config:
                return input_data.get(node.config["key"])
            return input_data

        if ntype == NodeType.CONDITION:
            # Evaluates condition and returns a boolean or string branch outcome
            field = node.config.get("field")
            expected = node.config.get("expected")
            val = input_data.get(field) if isinstance(input_data, dict) and field else input_data
            return val == expected if expected is not None else bool(val)

        if ntype == NodeType.LLM:
            if not self.llm:
                return f"[Mock LLM Output for: {input_data}]"
            model = node.config.get("model", "google/gemma-4-26b-a4b-it:free")
            try:
                from models.catalog import _lookup_admitted
                from models.policy import ModelRole

                _lookup_admitted(ModelRole.WORKFLOW, model)
            except Exception as err:
                raise ValueError(
                    f"Model '{model}' is not admitted for workflow role: {err}"
                ) from err
            system_prompt = node.config.get("system_prompt", "You are a workflow assistant.")
            user_content = str(input_data)

            req = GenerationRequest(
                model=model,
                messages=[
                    ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
                    ChatMessage(role=MessageRole.USER, content=user_content),
                ],
                temperature=node.config.get("temperature", 0.0),
            )
            res = await self.llm.generate(req)
            return res.text.strip()

        if ntype == NodeType.OUTPUT:
            return input_data

        # Default passthrough
        return input_data

    async def run(
        self,
        workflow: Workflow,
        initial_input: dict[str, Any],
    ) -> WorkflowRun:
        """Execute a workflow DAG from triggers to outputs."""
        run_id = f"wfrun-{uuid.uuid4().hex[:8]}"
        now = datetime.now(UTC).isoformat()

        wf_run = WorkflowRun(
            id=run_id,
            workspace_id=workflow.workspace_id,
            workflow_id=workflow.id,
            status=WorkflowRunStatus.RUNNING,
            initial_input=initial_input,
            created_at=now,
        )

        nodes_by_id = {n.id: n for n in workflow.nodes}
        # In-degree computation for topological ordering
        in_degree: dict[str, int] = {n.id: 0 for n in workflow.nodes}
        adj: dict[str, list[tuple[str, Any]]] = {n.id: [] for n in workflow.nodes}

        for edge in workflow.edges:
            if edge.source_node_id in nodes_by_id and edge.target_node_id in nodes_by_id:
                adj[edge.source_node_id].append((edge.target_node_id, edge.condition_value))
                in_degree[edge.target_node_id] += 1

        queue: deque[str] = deque([nid for nid, deg in in_degree.items() if deg == 0])
        node_outputs: dict[str, Any] = {}
        final_output: Any = None

        while queue:
            node_id = queue.popleft()
            node = nodes_by_id[node_id]

            # Determine input for this node
            # Inbound edges
            inbound_edges = [
                e for e in workflow.edges if e.target_node_id == node_id
            ]
            node_input: Any
            if not inbound_edges:
                node_input = initial_input
            elif len(inbound_edges) == 1:
                node_input = node_outputs.get(inbound_edges[0].source_node_id)
            else:
                node_input = {
                    e.source_node_id: node_outputs.get(e.source_node_id)
                    for e in inbound_edges
                }

            t_start = time.perf_counter()
            node_run = NodeRun(
                node_id=node_id,
                status=NodeRunStatus.RUNNING,
                input_data=node_input,
            )

            try:
                out = await self.execute_node(node, node_input, workflow.workspace_id)
                node_run.status = NodeRunStatus.COMPLETED
                node_run.output_data = out
                node_outputs[node_id] = out
                if node.type == NodeType.OUTPUT:
                    final_output = out
            except Exception as exc:
                node_run.status = NodeRunStatus.FAILED
                node_run.error = str(exc)
                wf_run.node_runs[node_id] = node_run
                wf_run.status = WorkflowRunStatus.FAILED
                break
            finally:
                node_run.latency_ms = (time.perf_counter() - t_start) * 1000.0
                wf_run.node_runs[node_id] = node_run

            # Process outgoing edges
            out_val = node_outputs[node_id]
            for target_id, cond in adj[node_id]:
                # If conditional edge, verify match
                if cond is not None:
                    # Compare condition
                    if cond != out_val:
                        # Skip this path
                        continue
                in_degree[target_id] -= 1
                if in_degree[target_id] == 0:
                    queue.append(target_id)
        else:
            if wf_run.status == WorkflowRunStatus.RUNNING:
                wf_run.status = WorkflowRunStatus.COMPLETED
                wf_run.final_output = final_output

        wf_run.finished_at = datetime.now(UTC).isoformat()
        return wf_run
