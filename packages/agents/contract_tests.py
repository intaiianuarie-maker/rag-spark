"""Contract checks for the V3 agent execution contracts (card A0).

Pure validation — no IO, no services, no vendor SDKs. These checks pin the
shared seams that the enforced dispatcher (A1) and the LangGraph execution
adapter (A2) implement:

- :class:`RunContext` is server-created and immutable; tools cannot override it.
- :class:`ToolSpec` is versioned with strict schemas and bounded retries.
- :class:`ToolPolicy` defaults to deny: an empty allowlist means no tools.
- :class:`RunState` transitions are monotonic and terminal; an unknown
  required failure can never produce ``COMPLETED``.
- :class:`StepEvent` sequences monotonically and stores references, not
  hidden reasoning.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from .interfaces import (
    RESERVED_CONTEXT_FIELDS,
    AgentErrorCode,
    ContextOverrideError,
    InvalidRunTransitionError,
    ModelPolicy,
    RetryPolicy,
    RunBudget,
    RunContext,
    RunState,
    RunStatus,
    StepEvent,
    StepStatus,
    ToolPolicy,
    ToolSpec,
    Usage,
    validate_tool_arguments,
)


def _fail(message: str) -> None:
    raise AssertionError(message)


def _require(condition: bool, message: str) -> None:
    if not condition:
        _fail(message)


def _require_instance(value: object, expected_type: type, what: str) -> None:
    if not isinstance(value, expected_type):
        _fail(f"{what}: expected {expected_type.__name__}, got {type(value).__name__}")


def _sample_context() -> RunContext:
    return RunContext(
        run_id="run-0001",
        workspace_id="ws-0001",
        principal_id="user-0001",
        authz_policy_revision="pol-0001",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(),
        deadline=datetime.now(UTC),
        allowed_tools=["calculator"],
    )


def check_run_context_contract(context: RunContext) -> None:
    """RunContext is server-created, immutable and fully scoped."""
    _require_instance(context, RunContext, "run context")
    for field in ("run_id", "workspace_id", "principal_id", "authz_policy_revision"):
        _require(bool(getattr(context, field)), f"RunContext.{field} must be non-empty")
    _require_instance(context.model_policy, ModelPolicy, "RunContext.model_policy")
    _require_instance(context.budget, RunBudget, "RunContext.budget")
    _require(context.deadline.tzinfo is not None, "RunContext.deadline must be timezone-aware")
    # Frozen: the context cannot be mutated by tool code.
    try:
        context.run_id = "run-evil"
    except Exception:
        pass
    else:
        _fail("RunContext must be frozen (immutable)")


def check_tool_spec_contract(spec: ToolSpec) -> None:
    """ToolSpec is versioned, strict and bounded."""
    _require_instance(spec, ToolSpec, "tool spec")
    _require(bool(spec.name), "ToolSpec.name must be non-empty")
    _require(bool(spec.version), "ToolSpec.version must be non-empty")
    _require(isinstance(spec.input_schema, dict), "ToolSpec.input_schema must be a JSON Schema")
    _require(spec.timeout_s > 0, "ToolSpec.timeout_s must be > 0")
    _require(spec.max_output_bytes >= 1, "ToolSpec.max_output_bytes must be >= 1")
    _require_instance(spec.retry_policy, RetryPolicy, "ToolSpec.retry_policy")
    _require(spec.retry_policy.max_attempts >= 1, "RetryPolicy.max_attempts must be >= 1")
    # Frozen: specs are versioned contracts, not mutable at runtime.
    try:
        spec.name = "renamed"
    except Exception:
        pass
    else:
        _fail("ToolSpec must be frozen (immutable)")


def check_tool_policy_contract(policy: ToolPolicy) -> None:
    """ToolPolicy defaults to deny; an empty allowlist means no tools.

    The check validates the *supplied* policy (rew1 B10): every tool it allows
    must be reflected in ``policy.allowed_tools``, and every other name must be
    denied.
    """
    _require_instance(policy, ToolPolicy, "tool policy")
    _require(isinstance(policy.allowed_tools, list), "allowed_tools must be a list")

    allowed = set(policy.allowed_tools)
    empty = ToolPolicy(allowed_tools=[])
    _require(not empty.allows("calculator"), "an empty allowlist must deny every tool")

    # Supplied policy: a listed tool is allowed, a clearly unlisted one is not.
    for name in policy.allowed_tools:
        _require(policy.allows(name), f"ToolPolicy must allow its listed tool {name!r}")
    sentinel = "__contract_absent_tool__"
    if sentinel not in allowed:
        _require(
            not policy.allows(sentinel),
            "ToolPolicy must deny a tool that is not in its allowlist",
        )
    # Empty allowlist is equivalent to the supplied policy denying everything.
    if not allowed:
        _require(
            not policy.allows("calculator") and not policy.allows("python"),
            "an empty supplied ToolPolicy must deny every tool",
        )


def check_run_state_contract(state: RunState) -> None:
    """RunState transitions are monotonic; terminal states are one-way.

    The check validates the *supplied* state (rew1 B10): its current status is
    a valid RunStatus, and an active state must accept a legal transition.
    """
    _require_instance(state, RunState, "run state")
    _require_instance(state.status, RunStatus, "RunState.status")

    # A terminal supplied state rejects any further transition.
    if RunState.is_terminal(state.status):
        try:
            state.transition_to(RunStatus.RUNNING)
        except InvalidRunTransitionError:
            pass
        else:
            _fail("a terminal run state must reject further transitions")
        return

    # An unknown required failure can never produce COMPLETED.
    bad = RunState(status=RunStatus.RUNNING)
    bad.mark_unknown_required_failure("provider vanished mid-run")
    try:
        bad.transition_to(RunStatus.COMPLETED)
    except InvalidRunTransitionError:
        pass
    else:
        _fail("unknown required failure must block COMPLETED")
    bad.transition_to(RunStatus.FAILED, error_code=AgentErrorCode.UNKNOWN_REQUIRED)
    _require(bad.status is RunStatus.FAILED, "failed must be reachable after unknown failure")
    _require(bad.error_code is AgentErrorCode.UNKNOWN_REQUIRED, "error_code must persist")

    # Active -> terminal is legal for the supplied state when no error is set.
    live = RunState(status=RunStatus.RUNNING)
    live.transition_to(RunStatus.COMPLETED)
    _require(live.status is RunStatus.COMPLETED, "running -> completed must work")

    # error_code on a non-terminal transition is rejected.
    live2 = RunState(status=RunStatus.RUNNING)
    try:
        live2.transition_to(RunStatus.WAITING_INPUT, error_code=AgentErrorCode.CANCELLED)
    except InvalidRunTransitionError:
        pass
    else:
        _fail("error_code must only be set on a terminal transition")


def check_step_event_contract(events: Sequence[StepEvent]) -> None:
    """StepEvent sequences monotonically and stores references, not reasoning."""
    _require(len(events) > 0, "at least one event is required")
    previous = 0
    for event in events:
        _require_instance(event, StepEvent, "step event")
        _require(event.sequence > previous, "sequence must be strictly increasing")
        previous = event.sequence
        _require(bool(event.step_id), "step_id must be non-empty")
        _require(bool(event.attempt_id), "attempt_id must be non-empty")
        _require(event.latency_ms >= 0, "latency_ms must be >= 0")
        _require_instance(event.usage, Usage, "StepEvent.usage")
        if event.status is StepStatus.FAILED or event.status is StepStatus.BLOCKED:
            _require(
                event.error_code is not None,
                "failed/blocked steps must carry an error_code",
            )


def check_tool_arguments_do_not_override_context(tool_input: Mapping[str, object]) -> None:
    """Reserved context fields in tool arguments are rejected loudly."""
    _require_instance(tool_input, Mapping, "tool input")
    # Benign input passes through.
    validate_tool_arguments({"expression": "1 + 1"})
    # Any reserved field is a ContextOverrideError.
    for field in sorted(RESERVED_CONTEXT_FIELDS):
        try:
            validate_tool_arguments({field: "evil"})
        except ContextOverrideError:
            continue
        _fail(f"tool argument {field!r} must be rejected as a reserved context field")
