"""Workflow Engine domain interfaces (Phase 13 / Section 30).

Implements DAG-based workflow execution models: Node, Edge, Workflow, NodeRun, WorkflowRun.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class NodeType(StrEnum):
    TRIGGER = "trigger"
    RETRIEVER = "retriever"
    LLM = "llm"
    AGENT = "agent"
    TOOL = "tool"
    CONDITION = "condition"
    TRANSFORM = "transform"
    HTTP = "http"
    OUTPUT = "output"


class NodeRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class WorkflowRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowNode(BaseModel):
    """A single operational block in a workflow DAG."""

    id: str
    type: NodeType
    name: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowEdge(BaseModel):
    """A directed dependency between two workflow nodes."""

    id: str
    source_node_id: str
    target_node_id: str
    condition_value: Any | None = None


class Workflow(BaseModel):
    """Definition of a runnable workflow DAG."""

    id: str
    workspace_id: str
    name: str
    description: str = ""
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)


class NodeRun(BaseModel):
    """Execution trace of an individual node in a workflow run."""

    node_id: str
    status: NodeRunStatus = NodeRunStatus.PENDING
    input_data: Any = None
    output_data: Any = None
    error: str | None = None
    latency_ms: float = 0.0


class WorkflowRun(BaseModel):
    """Execution instance of a workflow."""

    id: str
    workspace_id: str
    workflow_id: str
    status: WorkflowRunStatus = WorkflowRunStatus.PENDING
    node_runs: dict[str, NodeRun] = Field(default_factory=dict)
    initial_input: dict[str, Any] = Field(default_factory=dict)
    final_output: Any = None
    created_at: str = ""
    finished_at: str | None = None
