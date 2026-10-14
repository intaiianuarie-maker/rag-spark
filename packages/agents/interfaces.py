"""Agent Runtime interfaces (Phase 11 / Sections 25-27) + V3 typed contracts.

Separates agents from pure RAG:
An agent has instructions, model, tools, knowledge access, budget, and max steps.
Stores every step for full observability, debugging, and replay.

V3 (card A0) adds the product execution contracts: :class:`RunContext`,
:class:`RunState`, :class:`ToolSpec`, :class:`ToolPolicy`, :class:`StepEvent`,
:class:`Usage` and the :class:`AgentErrorCode` taxonomy. These are the seams the
enforced dispatcher (A1) and the LangGraph execution adapter (A2) implement;
typing them here keeps every adapter behind one shared contract.

Prototype note: :class:`AgentRun` / :class:`AgentStep` remain the in-memory
prototype trace. The durable product trace is :class:`StepEvent` (A2).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


# --------------------------------------------------------------------------------------
# V3 typed execution contracts (card A0)
# --------------------------------------------------------------------------------------


class AgentErrorCode(StrEnum):
    """Stable error codes for agent run and step failures.

    Codes are persisted on :class:`RunState` / :class:`StepEvent` so operators
    and the UI can branch on a stable taxonomy instead of parsing message text.
    """

    TOOL_NOT_ALLOWED = "tool_not_allowed"
    TOOL_NOT_FOUND = "tool_not_found"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_OUTPUT_TOO_LARGE = "tool_output_too_large"
    TOOL_SCHEMA_INVALID = "tool_schema_invalid"
    BUDGET_EXCEEDED = "budget_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"
    PROVIDER_ERROR = "provider_error"
    SCHEMA_INVALID = "schema_invalid"
    WORKSPACE_MISMATCH = "workspace_mismatch"
    SOURCE_REVOKED = "source_revoked"
    UNKNOWN_REQUIRED = "unknown_required"
    #: T-R02: execution was requested but a required piece of production wiring
    #: is absent, so nothing could be run. Terminal and never a completion — the
    #: API must not answer with a fabricated result when this is the truth.
    UNAVAILABLE = "unavailable"


class RunStatus(StrEnum):
    """Durable run lifecycle states.

    Active: ``queued`` -> ``running`` -> ``waiting_input`` / ``waiting_approval``.
    Terminal: ``completed`` / ``partial`` / ``failed`` / ``cancelled`` /
    ``limit_exceeded``. Terminal transitions are one-way (see
    :meth:`RunState.transition_to`).
    """

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LIMIT_EXCEEDED = "limit_exceeded"


class StepStatus(StrEnum):
    """Per-step execution status."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class Usage(BaseModel):
    """Token/tool usage for one step or a whole run.

    ``cost_usd`` is optional because money caps require pinned pricing; when
    pricing is unknown the field stays ``None`` and the run must not treat the
    call as free (invariant 3, V3 plan section 4).
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    cost_usd: float | None = None


class RunBudget(BaseModel):
    """Hard resource limits for one run. Enforced before every model/tool call."""

    model_config = ConfigDict(frozen=True)

    max_steps: int = 10
    max_tokens: int = 100_000
    max_tool_calls: int = 50
    max_wall_clock_s: float = 300.0
    max_concurrent_tools: int = 1

    @model_validator(mode="after")
    def _validate_bounds(self) -> RunBudget:
        if self.max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        if self.max_tool_calls < 0:
            raise ValueError("max_tool_calls must be >= 0")
        if self.max_wall_clock_s <= 0:
            raise ValueError("max_wall_clock_s must be > 0")
        if self.max_concurrent_tools < 1:
            raise ValueError("max_concurrent_tools must be >= 1")
        return self


class ModelPolicy(BaseModel):
    """Server-side model selection policy. Tools cannot override it."""

    model_config = ConfigDict(frozen=True)

    allowed_models: list[str] = Field(default_factory=list)
    default_model: str = ""
    max_output_tokens: int = 4096
    temperature: float = 0.0

    @model_validator(mode="after")
    def _validate(self) -> ModelPolicy:
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be >= 1")
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be within [0.0, 2.0]")
        return self


class RetryPolicy(BaseModel):
    """Retry behaviour for a tool call. Only classified transient errors retry."""

    model_config = ConfigDict(frozen=True)

    max_attempts: int = 1
    initial_backoff_s: float = 0.0
    backoff_multiplier: float = 2.0
    max_backoff_s: float = 30.0
    retryable_error_codes: list[AgentErrorCode] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> RetryPolicy:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.initial_backoff_s < 0:
            raise ValueError("initial_backoff_s must be >= 0")
        if self.backoff_multiplier < 1.0:
            raise ValueError("backoff_multiplier must be >= 1.0")
        if self.max_backoff_s < self.initial_backoff_s:
            raise ValueError("max_backoff_s must be >= initial_backoff_s")
        return self


class ToolClass(StrEnum):
    """Read vs write capability class for a tool."""

    READ = "read"
    WRITE = "write"


class ToolSpec(BaseModel):
    """Versioned, strict tool contract.

    ``input_schema`` / ``result_schema`` are JSON Schemas with
    ``additionalProperties: False`` so dispatch rejects unexpected arguments
    instead of silently forwarding them to the tool.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    version: str = "1"
    description: str = ""
    input_schema: dict[str, Any]
    result_schema: dict[str, Any] = Field(default_factory=dict)
    tool_class: ToolClass = ToolClass.READ
    permissions: list[str] = Field(default_factory=list)
    allowed_resources: list[str] = Field(default_factory=list)
    timeout_s: float = 30.0
    max_output_bytes: int = 65_536
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)

    @model_validator(mode="after")
    def _validate(self) -> ToolSpec:
        if not self.name:
            raise ValueError("name must be non-empty")
        if not self.version:
            raise ValueError("version must be non-empty")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if self.max_output_bytes < 1:
            raise ValueError("max_output_bytes must be >= 1")
        for schema_field in ("input_schema", "result_schema"):
            schema = getattr(self, schema_field)
            if not isinstance(schema, dict):
                raise ValueError(f"{schema_field} must be a JSON Schema object")
            if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
                raise ValueError(
                    f"{schema_field} must set additionalProperties=false for strict dispatch"
                )
        return self


class ToolPolicy(BaseModel):
    """Allowlist applied at dispatch time.

    An empty ``allowed_tools`` means NO tools are available — the default is
    deny, not allow. Dispatch must consult this policy even when the prompt
    claims a tool is available (V3 plan section 3, ToolSpec row).
    """

    model_config = ConfigDict(frozen=True)

    allowed_tools: list[str] = Field(default_factory=list)

    def allows(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools


class RunContext(BaseModel):
    """Server-created, immutable execution context for one run.

    Tools cannot override any of these fields with model-supplied arguments;
    reserved context fields are rejected at dispatch (see
    :func:`validate_tool_arguments`). Reauthorization happens on every
    execution/resume and source read — the context is rebuilt server-side, never
    trusted from the client.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    workspace_id: str
    principal_id: str
    authz_policy_revision: str
    model_policy: ModelPolicy
    budget: RunBudget
    deadline: datetime
    allowed_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> RunContext:
        if not self.run_id or not self.workspace_id or not self.principal_id:
            raise ValueError("run_id, workspace_id and principal_id must be non-empty")
        if not self.authz_policy_revision:
            raise ValueError("authz_policy_revision must be non-empty")
        if self.deadline.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")
        return self


#: Fields a tool argument payload may never set — they come from the server.
RESERVED_CONTEXT_FIELDS: frozenset[str] = frozenset(
    {
        "run_id",
        "workspace_id",
        "principal_id",
        "authz_policy_revision",
        "model_policy",
        "budget",
        "deadline",
        "allowed_tools",
    }
)


class ContextOverrideError(ValueError):
    """Raised when tool arguments try to override reserved run context."""


def validate_tool_arguments(tool_input: Mapping[str, Any]) -> Mapping[str, Any]:
    """Reject model-supplied arguments that override reserved context fields.

    Dispatch runs this before any tool executes; a violation is a
    ``ContextOverrideError`` and the step is recorded as ``BLOCKED`` with
    :attr:`AgentErrorCode.TOOL_NOT_ALLOWED`.
    """
    if not isinstance(tool_input, Mapping):
        raise ContextOverrideError("tool input must be a mapping of argument names to values")
    overlap = RESERVED_CONTEXT_FIELDS & set(tool_input)
    if overlap:
        raise ContextOverrideError(
            f"tool arguments override reserved context fields: {sorted(overlap)}"
        )
    return tool_input


class StepEvent(BaseModel):
    """One durable, ordered event in a run trace.

    Stores action summaries and safe tool I/O *references*, never hidden model
    reasoning. ``sequence`` is monotonically increasing within a run;
    ``step_id`` / ``attempt_id`` make retries addressable. Secrets are redacted
    and payloads bounded before the event is stored.
    """

    sequence: int
    step_id: str
    attempt_id: str
    action_summary: str
    tool_name: str | None = None
    tool_input_ref: str | None = None
    tool_output_ref: str | None = None
    status: StepStatus = StepStatus.STARTED
    error_code: AgentErrorCode | None = None
    latency_ms: float = 0.0
    usage: Usage = Field(default_factory=Usage)
    timestamp: str = ""

    @model_validator(mode="after")
    def _validate(self) -> StepEvent:
        if self.sequence < 1:
            raise ValueError("sequence must be >= 1 (monotonic within a run)")
        if not self.step_id or not self.attempt_id:
            raise ValueError("step_id and attempt_id must be non-empty")
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be >= 0")
        if self.status is StepStatus.FAILED or self.status is StepStatus.BLOCKED:
            if self.error_code is None:
                raise ValueError("failed/blocked steps must carry an error_code")
        return self


class InvalidRunTransitionError(ValueError):
    """Raised for illegal or non-monotonic run status transitions."""


_TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.LIMIT_EXCEEDED,
    }
)

_ACTIVE_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.WAITING_INPUT,
        RunStatus.WAITING_APPROVAL,
    }
)


class RunState(BaseModel):
    """Durable run status with monotonic, validated transitions.

    Terminal transitions are one-way: once a run reaches a terminal status it
    never leaves it. An unknown required failure can never produce
    ``COMPLETED`` — that status is reserved for runs that finished every
    required action under budget (V3 plan section 3, RunState row).
    """

    status: RunStatus = RunStatus.QUEUED
    error_code: AgentErrorCode | None = None
    error_detail: str | None = None
    unknown_required_failure: bool = False

    @classmethod
    def is_terminal(cls, status: RunStatus) -> bool:
        return status in _TERMINAL_STATUSES

    @classmethod
    def is_active(cls, status: RunStatus) -> bool:
        return status in _ACTIVE_STATUSES

    def transition_to(
        self,
        status: RunStatus,
        *,
        error_code: AgentErrorCode | None = None,
        error_detail: str | None = None,
    ) -> None:
        """Move to ``status``, enforcing monotonic terminal transitions."""
        if self.is_terminal(self.status):
            raise InvalidRunTransitionError(
                f"run is already terminal ({self.status}); cannot move to {status}"
            )
        if status is RunStatus.COMPLETED and self.unknown_required_failure:
            raise InvalidRunTransitionError(
                "an unknown required failure occurred; the run cannot be marked completed"
            )
        if error_code is not None and status not in _TERMINAL_STATUSES:
            raise InvalidRunTransitionError(
                "error_code is only valid on a terminal transition"
            )
        self.status = status
        self.error_code = error_code
        self.error_detail = error_detail

    def mark_unknown_required_failure(self, detail: str) -> None:
        """Record that a required action failed for an unknown reason.

        After this, ``COMPLETED`` is unreachable; the run must end ``FAILED`` or
        ``PARTIAL`` with :attr:`AgentErrorCode.UNKNOWN_REQUIRED`.
        """
        self.unknown_required_failure = True
        self.error_code = AgentErrorCode.UNKNOWN_REQUIRED
        self.error_detail = detail

    def request_cancel(self) -> bool:
        """Request cancellation — terminal-aware, byte-identical no-op if already terminal.

        If the run is already terminal (``COMPLETED``/``CANCELLED``/``FAILED``/etc.),
        this is a clean no-op: returns ``False`` and changes nothing, so a
        ``cancel-after-COMPLETED`` leaves state and record byte-identical.
        Otherwise transitions to ``CANCELLED`` with ``AgentErrorCode.CANCELLED``
        and returns ``True``. Monotonicity is enforced by ``transition_to``.
        """
        if self.is_terminal(self.status):
            return False
        try:
            self.transition_to(
                RunStatus.CANCELLED,
                error_code=AgentErrorCode.CANCELLED,
                error_detail="run cancelled",
            )
            return True
        except InvalidRunTransitionError:
            return False

    def is_cancelled(self) -> bool:
        return self.status == RunStatus.CANCELLED
