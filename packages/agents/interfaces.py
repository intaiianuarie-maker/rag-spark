"""Agent Runtime interfaces (Phase 11 / Sections 25-27).

Separates agents from pure RAG:
An agent has instructions, model, tools, knowledge access, budget, and max steps.
Stores every step for full observability, debugging, and replay.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AgentStepType(StrEnum):
    REASONING = "reasoning"
    PLANNING = "planning"
    RETRIEVAL = "retrieval"
    TOOL_CALL = "tool_call"
    TOOL_OUTPUT = "tool_output"
    FINAL_ANSWER = "final_answer"
    ERROR = "error"


class AgentRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    BUDGET_EXCEEDED = "budget_exceeded"


class AgentStep(BaseModel):
    """An individual execution step within an agent run."""

    step_number: int
    type: AgentStepType
    content: str = ""
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: Any | None = None
    latency_ms: float = 0.0
    timestamp: str = ""


class AgentRun(BaseModel):
    """A durable execution trace of an agent fulfilling a goal."""

    id: str
    workspace_id: str
    goal: str
    model: str
    status: AgentRunStatus = AgentRunStatus.PENDING
    steps: list[AgentStep] = Field(default_factory=list)
    final_output: str | None = None
    total_tokens: int = 0
    error: str | None = None
    created_at: str = ""
    finished_at: str | None = None


class AgentConfig(BaseModel):
    """Configuration governing an agent run."""

    instructions: str = (
        "You are a helpful knowledge agent. Solve user tasks step-by-step using tools."
    )
    model: str = "gpt-4o-mini"
    max_steps: int = 10
    budget_tokens: int = 100000
    allowed_tools: list[str] = Field(default_factory=list)
    temperature: float = 0.0
