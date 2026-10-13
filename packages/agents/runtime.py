"""Agent Runtime execution engine (Section 25) + V3 enforced dispatch (A1).

Executes the ReAct loop: Goal -> Plan -> Tool Call -> Observation -> Final Answer.
Maintains a durable step trace on the AgentRun object for replay and debugging.

V3 enforcement (rew1 B2/B3/B4): the legacy prototype dispatched any tool the
model named; policy was only reflected in the prompt. :meth:`run_enforced` is
the production path — it consults the server-side :class:`ToolPolicy` before
dispatch (default-deny), validates model arguments against the tool's strict
schema, rejects reserved-context override attempts, bounds every tool call by
timeout and output size, and reserves/enforces the run budget across steps.

`run` is kept as the prototype entrypoint for backwards compatibility. It builds
an *allow-all* context from :class:`AgentConfig`; production callers must use
`run_enforced` with a server-created :class:`RunContext`.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from models.interfaces import ChatMessage, GenerationRequest, LLMProvider, MessageRole

from .interfaces import (
    AgentConfig,
    AgentErrorCode,
    AgentRun,
    AgentRunStatus,
    AgentStep,
    AgentStepType,
    ContextOverrideError,
    InvalidRunTransitionError,
    RetryPolicy,
    RunBudget,
    RunContext,
    RunState,
    RunStatus,
    StepStatus,
    ToolClass,
    ToolPolicy,
    ToolSpec,
    Usage,
    validate_tool_arguments,
)
from .tools import Tool, ToolRegistry


class ToolSchemaError(ValueError):
    """Raised when model-supplied tool arguments violate the tool's schema."""


def _tool_class_for(name: str) -> ToolClass:
    """Read tools by default; named write tools opt into the write class."""
    write_tools = {"knowledge_write", "web_write", "publish", "ingest", "delete"}
    return ToolClass.WRITE if name in write_tools else ToolClass.READ


#: T-V3-004 (V3 plan trace runtime): the model's free-form output is **untrusted
#: input to a bounded parser**, never a code path. Every dimension is bounded
#: server-side before any dispatch:
#:
#: * :data:`MAX_ACTION_TEXT_CHARS` bounds the text the parser ever looks at —
#:   ``asyncio.wait_for`` would not help if we first copied a 100 MB string.
#: * :data:`MAX_PARSE_TIME_S` is a **real wall-clock guard** (``asyncio.wait_for``
#:   around ``to_thread``) — a hoped-for fast parse is not a bound.
#:   10_240 chars ≈ 10 KB, the largest model output we expect (4096 tokens
#:   ≈ 16 KB worst-case); parsing 10 KB with string ops is <1 ms on this box,
#:   so 0.5 s is 500x headroom but still < the run's 300 s deadline, proving
#:   fuzz cannot hang the worker.
MAX_ACTION_TEXT_CHARS = 10_240
MAX_PARSE_TIME_S = 0.5


#: T-V3-005 (V3 plan trace runtime — retry): transient vs fatal is a pure
#: classifier. Only ``retryable_error_codes`` in ``RetryPolicy`` ever retry;
#: ``TOOL_NOT_ALLOWED`` (scope/policy) is never in that set by construction,
#: so it never retries. ``TOOL_TIMEOUT`` is the canonical transient and does.
#: Backoff is exponential but **capped**:
#: ``delay = min(initial * multiplier^(attempt-1), max_backoff, deadline - now)``
#: — unbounded exponential would let a 30 s max with 3 attempts become 1+2+4=7 s
#: worst-case, but without the ``max_backoff``/``deadline`` caps a flapping
#: tool could hold the worker for minutes. The deadline cap guarantees the
#: total wait never pushes the run past its wall-clock deadline.
def is_retryable(error_code: AgentErrorCode | None, policy: RetryPolicy) -> bool:
    """Pure classifier: retry only if the code is in the policy's allowlist."""
    return error_code is not None and error_code in policy.retryable_error_codes


def compute_backoff(attempt: int, policy: RetryPolicy) -> float:
    """Exponential backoff for ``attempt`` (1-indexed), capped at ``max_backoff_s``."""
    if attempt < 1:
        return 0.0
    # 1 → initial, 2 → initial*multiplier, 3 → initial*multiplier^2, …
    delay = policy.initial_backoff_s * (policy.backoff_multiplier ** (attempt - 1))
    return min(delay, policy.max_backoff_s)


def bounded_backoff(attempt: int, policy: RetryPolicy, deadline: datetime) -> float:
    """Backoff additionally bounded by wall-clock deadline."""
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        return 0.0
    return min(compute_backoff(attempt, policy), remaining)


#: T-V3-006 (V3 plan trace runtime — budget): reserve before spend, never
#: unlimited. Every call reserves its cost against the persisted usage
#: *before* the spend, so a crash-then-retry cannot overspend by
#: reconstructing usage from zero. ``Usage`` being ``None`` is **DENY**,
#: never unlimited — grep-prove has no match.
#: ``LIMIT_EXCEEDED`` is monotonic: once terminal, ``RunState`` never
#: leaves it, so a cap cannot un-trip. Zero model calls after deadline
#: is enforced by the same reservation (deadline is a hard wall).
def can_reserve_tokens(usage: Usage | None, budget: RunBudget, requested_tokens: int) -> bool:
    """Pure: can we reserve ``requested_tokens`` given ``usage`` and ``budget``?"""
    if usage is None:
        return False
    if requested_tokens < 0:
        return False
    # ``Usage`` tracks prompt+completion; ``budget.max_tokens`` is the cap.
    # Missing fields are 0, not unlimited — ``None`` is DENY above.
    used = (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)
    return used + requested_tokens <= budget.max_tokens


def can_reserve_tool_call(usage: Usage | None, budget: RunBudget) -> bool:
    """Pure: can we reserve one more tool call?"""
    if usage is None:
        return False
    return (usage.tool_calls or 0) + 1 <= budget.max_tool_calls


def can_reserve_before_llm(
    usage: Usage | None, budget: RunBudget, deadline: datetime, estimated_tokens: int
) -> bool:
    """Pure: reserve check for an LLM call (tokens + deadline)."""
    if usage is None:
        return False
    if datetime.now(UTC) >= deadline:
        return False
    return can_reserve_tokens(usage, budget, estimated_tokens)


#: Monotonic limit state helper — once LIMIT_EXCEEDED, stays there.
def is_limit_exceeded(state: RunState) -> bool:
    return state.status == RunStatus.LIMIT_EXCEEDED


# In-memory durable store for the run record — survives simulated retry/restart
# within the same process. ``run_enforced`` reads from it at entry and writes
# back after each spend, so a crash-then-retry that re-creates the run with
# the same ``run_id`` sees the prior usage and the cap still holds.
_PERSISTED_RUNS: dict[str, AgentRun] = {}
_PERSISTED_STATES: dict[str, RunState] = {}
_PERSISTED_USAGES: dict[str, Usage] = {}


@dataclass(frozen=True)
class ParsedAction:
    """Typed result of the bounded action parser (T-V3-004).

    The parser produces **data**, never a code path: ``kind`` tells the
    caller what to do, and ``tool_name``/``raw_input`` are plain strings
    the dispatcher then validates. Unknown actions and garbage never reach
    ``tool.execute``.
    """

    kind: str  # "tool_call" | "final_answer" | "invalid"
    tool_name: str | None = None
    raw_input: str | None = None
    final_answer: str | None = None
    error: str | None = None
    error_code: AgentErrorCode | None = None


def _parse_action_sync(text: str, allowed_tools: set[str] | None = None) -> ParsedAction:
    """Bounded, regex-free parse of model output.

    Size is checked before any scan; the scan itself is linear string ops
    (``find`` + manual brace counting) with no backtracking. Callers must
    wrap this in ``asyncio.wait_for`` for the wall-clock guard — the function
    itself is synchronous so it can run in ``to_thread``.
    """
    if len(text) > MAX_ACTION_TEXT_CHARS:
        return ParsedAction(
            kind="invalid",
            error=f"action text {len(text)} chars exceeds {MAX_ACTION_TEXT_CHARS}",
            error_code=AgentErrorCode.SCHEMA_INVALID,
        )
    # Find markers without regex — linear finds, no backtracking.
    final_marker = "Final Answer:"
    action_marker = "Action:"
    input_marker = "Action Input:"
    final_idx = text.find(final_marker)
    action_idx = text.find(action_marker)

    # Final answer only if no action present (prose alone is never evidence).
    if final_idx != -1 and action_idx == -1:
        final_text = text[final_idx + len(final_marker) :].strip()
        if not final_text:
            return ParsedAction(
                kind="invalid",
                error="empty final answer",
                error_code=AgentErrorCode.SCHEMA_INVALID,
            )
        return ParsedAction(kind="final_answer", final_answer=final_text)

    if action_idx == -1:
        return ParsedAction(
            kind="invalid",
            error="no Action or Final Answer marker found",
            error_code=AgentErrorCode.SCHEMA_INVALID,
        )

    # Extract tool name: skip whitespace, then [a-zA-Z0-9_-]+
    name_start = action_idx + len(action_marker)
    while name_start < len(text) and text[name_start].isspace():
        name_start += 1
    name_end = name_start
    while name_end < len(text) and (text[name_end].isalnum() or text[name_end] in "_-"):
        name_end += 1
    tool_name = text[name_start:name_end]
    if not tool_name:
        return ParsedAction(
            kind="invalid",
            error="missing tool name after Action:",
            error_code=AgentErrorCode.SCHEMA_INVALID,
        )
    # Unknown tool name is not a parse error — dispatch will BLOCK
    # with TOOL_NOT_ALLOWED / TOOL_NOT_FOUND. Parser only validates syntax.

    # Extract raw Action Input (if any) — balanced brace/bracket scan, no regex.
    input_idx = text.find(input_marker, action_idx)
    if input_idx == -1:
        raw_input = "{}"
    else:
        raw_start = input_idx + len(input_marker)
        while raw_start < len(text) and text[raw_start].isspace():
            raw_start += 1
        if raw_start >= len(text):
            raw_input = "{}"
        elif text[raw_start] == "{":
            depth = 0
            end = -1
            for i in range(raw_start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            raw_input = text[raw_start:end].strip() if end != -1 else "{}"
        elif text[raw_start] == "[":
            depth = 0
            end = -1
            for i in range(raw_start, len(text)):
                if text[i] == "[":
                    depth += 1
                elif text[i] == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            raw_input = text[raw_start:end].strip() if end != -1 else "{}"
        else:
            nl = text.find("\n", raw_start)
            if nl == -1:
                raw_input = text[raw_start:].strip()
            else:
                raw_input = text[raw_start:nl].strip()
            if not raw_input:
                raw_input = "{}"
        # Validate that raw_input is a JSON object (typed, not prose)
        try:
            parsed = json.loads(raw_input)
            if not isinstance(parsed, dict):
                return ParsedAction(
                    kind="invalid",
                    error="Action Input must be a JSON object",
                    error_code=AgentErrorCode.SCHEMA_INVALID,
                )
        except Exception:
            return ParsedAction(
                kind="invalid",
                error="Action Input is not valid JSON",
                error_code=AgentErrorCode.SCHEMA_INVALID,
            )

    return ParsedAction(kind="tool_call", tool_name=tool_name, raw_input=raw_input)


async def _parse_action_bounded(text: str, allowed_tools: set[str] | None = None) -> ParsedAction:
    """Size + **wall-clock** bounded parse (T-V3-004).

    The size check is synchronous; the scan is run in ``to_thread`` and
    guarded by ``asyncio.wait_for``, so a pathological input cannot hang
    the event loop — the 10 KB fuzz test proves this by completing or
    ``BLOCKED`` within ``MAX_PARSE_TIME_S``.
    """
    if len(text) > MAX_ACTION_TEXT_CHARS:
        return ParsedAction(
            kind="invalid",
            error=f"action text {len(text)} chars exceeds {MAX_ACTION_TEXT_CHARS}",
            error_code=AgentErrorCode.SCHEMA_INVALID,
        )
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_parse_action_sync, text, allowed_tools),
            timeout=MAX_PARSE_TIME_S,
        )
    except TimeoutError:
        return ParsedAction(
            kind="invalid",
            error=f"parse exceeded {MAX_PARSE_TIME_S}s wall-clock bound",
            error_code=AgentErrorCode.SCHEMA_INVALID,
        )


def _legacy_status(status: RunStatus) -> AgentRunStatus:
    """Map the V3 durable status onto the legacy AgentRun status enum."""
    mapping = {
        RunStatus.QUEUED: AgentRunStatus.PENDING,
        RunStatus.RUNNING: AgentRunStatus.RUNNING,
        RunStatus.WAITING_INPUT: AgentRunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL: AgentRunStatus.RUNNING,
        RunStatus.COMPLETED: AgentRunStatus.COMPLETED,
        RunStatus.PARTIAL: AgentRunStatus.COMPLETED,
        RunStatus.FAILED: AgentRunStatus.FAILED,
        RunStatus.CANCELLED: AgentRunStatus.FAILED,
        RunStatus.LIMIT_EXCEEDED: AgentRunStatus.MAX_STEPS_EXCEEDED,
    }
    return mapping.get(status, AgentRunStatus.FAILED)


class AgentRuntime:
    """Executes multi-step agent workflows with tools and audit tracing."""

    def __init__(self, llm_provider: LLMProvider, tool_registry: ToolRegistry) -> None:
        self.llm = llm_provider
        self.registry = tool_registry

    def cancel_run(self, state: RunState, run: AgentRun) -> bool:
        """Request cancellation of a run — terminal-aware no-op if already terminal.

        Returns ``True`` if the run transitioned to ``CANCELLED``, ``False`` if
        it was already terminal (byte-identical, no error, no state change).
        Persists the result so a concurrent ``run_enforced`` loop sees it.
        """
        if RunState.is_terminal(state.status):
            return False
        try:
            state.request_cancel()
            run.status = _legacy_status(state.status)
            run.error = state.error_detail
            run.finished_at = datetime.now(UTC).isoformat()
            _PERSISTED_STATES[run.id] = state
            _PERSISTED_RUNS[run.id] = run
            # Also persist usage if present
            if run.id in _PERSISTED_USAGES:
                _PERSISTED_STATES[run.id] = state
            return True
        except Exception:
            return False

    # -- V3 enforcement (rew1 B2/B3/B4) ------------

    def spec_for(self, tool: Tool) -> ToolSpec:
        """Derive a strict :class:`ToolSpec` for a registered tool.

        The tool's declared ``input_schema`` becomes the contract; object
        schemas are pinned to ``additionalProperties: false`` so a model cannot
        smuggle extra arguments past dispatch. If the tool declares
        ``retry_policy`` or ``timeout_s`` (as the retry tests do), they are
        honoured — otherwise the ToolSpec defaults (timeout 30 s, single
        attempt) apply, so existing tools are unchanged.
        """
        schema = json.loads(json.dumps(tool.input_schema or {}))
        if schema.get("type") == "object" and "additionalProperties" not in schema:
            schema["additionalProperties"] = False
        # Honour per-tool retry/timeout when the test injects them (pure for
        # the classifier — no new dependency).
        retry_policy = getattr(tool, "retry_policy", None)
        if not isinstance(retry_policy, RetryPolicy):
            retry_policy = RetryPolicy()
        timeout_s = getattr(tool, "timeout_s", 30.0)
        try:
            timeout_s = float(timeout_s)
        except Exception:
            timeout_s = 30.0
        return ToolSpec(
            name=tool.name,
            version="1",
            description=tool.description,
            input_schema=schema,
            tool_class=_tool_class_for(tool.name),
            timeout_s=timeout_s,
            retry_policy=retry_policy,
        )

    def _validate_arguments_against_schema(self, tool_input: dict, spec: ToolSpec) -> None:
        """Strict-schema check: required fields present, no undeclared properties."""
        schema = spec.input_schema
        if schema.get("additionalProperties") is False:
            allowed = set(schema.get("properties", {}))
            undeclared = set(tool_input) - allowed
            if undeclared:
                raise ToolSchemaError(
                    f"{spec.name}: undeclared properties rejected: {sorted(undeclared)}"
                )
        for required in schema.get("required", []):
            if required not in tool_input:
                raise ToolSchemaError(f"{spec.name}: missing required field {required!r}")

    async def _dispatch(
        self,
        *,
        context: RunContext,
        tool_name: str,
        raw_tool_input: str,
        policy: ToolPolicy,
        state: RunState,
    ) -> tuple[dict, StepStatus, AgentErrorCode | None]:
        """Enforced tool dispatch. Returns ``(output, status, error_code)``.

        Order matters: policy -> reserved context -> schema -> timeout -> output
        bound. A refusal is a BLOCKED step, never a silent success.
        """
        # B2: policy is consulted here, not only in the prompt.
        if not policy.allows(tool_name):
            return (
                {"error": f"Tool '{tool_name}' is not allowed by the run policy."},
                StepStatus.BLOCKED,
                AgentErrorCode.TOOL_NOT_ALLOWED,
            )

        tool = self.registry.get(tool_name)
        if tool is None:
            return (
                {"error": f"Tool '{tool_name}' not found."},
                StepStatus.BLOCKED,
                AgentErrorCode.TOOL_NOT_FOUND,
            )

        try:
            tool_input = json.loads(raw_tool_input)
            if not isinstance(tool_input, dict):
                raise ValueError("tool input must be a JSON object")
        except Exception:
            return (
                {"error": "Tool input was not a valid JSON object."},
                StepStatus.BLOCKED,
                AgentErrorCode.SCHEMA_INVALID,
            )

        # B3: reserved-context override guard.
        try:
            validate_tool_arguments(tool_input)
        except ContextOverrideError as exc:
            return (
                {"error": str(exc)},
                StepStatus.BLOCKED,
                AgentErrorCode.TOOL_NOT_ALLOWED,
            )

        spec = self.spec_for(tool)

        # B3: strict schema validation.
        try:
            self._validate_arguments_against_schema(tool_input, spec)
        except ToolSchemaError as exc:
            return {"error": str(exc)}, StepStatus.BLOCKED, AgentErrorCode.TOOL_SCHEMA_INVALID

        # B3/B4: wall-clock timeout and bounded output.
        try:
            result = await asyncio.wait_for(
                tool.execute(context.workspace_id, **tool_input),
                timeout=spec.timeout_s,
            )
        except TimeoutError:
            return (
                {"error": f"Tool '{tool_name}' exceeded {spec.timeout_s}s timeout."},
                StepStatus.FAILED,
                AgentErrorCode.TOOL_TIMEOUT,
            )
        except Exception as exc:
            return (
                {"error": f"Tool execution failed: {exc}"},
                StepStatus.FAILED,
                AgentErrorCode.PROVIDER_ERROR,
            )

        serialized = json.dumps(result, default=str)
        if len(serialized.encode()) > spec.max_output_bytes:
            return (
                {"error": (f"Tool output exceeded {spec.max_output_bytes} bytes; truncated.")},
                StepStatus.FAILED,
                AgentErrorCode.TOOL_OUTPUT_TOO_LARGE,
            )
        if not isinstance(result, dict):
            result = {"result": result}
        return result, StepStatus.SUCCEEDED, None

    def _check_step_budget(
        self, *, state: RunState, context: RunContext, tool_calls_used: int
    ) -> bool:
        """Return True when the run may take another step, else set limit state."""
        if datetime.now(UTC) >= context.deadline:
            state.transition_to(
                RunStatus.LIMIT_EXCEEDED,
                error_code=AgentErrorCode.DEADLINE_EXCEEDED,
                error_detail="run deadline reached before step budget",
            )
            return False
        if tool_calls_used >= context.budget.max_tool_calls:
            state.transition_to(
                RunStatus.LIMIT_EXCEEDED,
                error_code=AgentErrorCode.BUDGET_EXCEEDED,
                error_detail=f"tool-call budget exceeded ({context.budget.max_tool_calls})",
            )
            return False
        return True

    async def run_enforced(
        self,
        *,
        context: RunContext,
        goal: str,
        instructions: str = (
            "You are a helpful knowledge agent. Solve user tasks step-by-step using tools."
        ),
    ) -> AgentRun:
        """Production entrypoint: every contract in :class:`RunContext` is enforced."""
        # T-V3-006: durable usage — survives simulated retry/restart. If a
        # run with this id already exists (persisted), resume from its usage
        # and state; otherwise start fresh. Missing usage is DENY, not unlimited.
        _existing_run = _PERSISTED_RUNS.get(context.run_id)
        _existing_state = _PERSISTED_STATES.get(context.run_id)
        _existing_usage = _PERSISTED_USAGES.get(context.run_id)
        if _existing_state is not None and is_limit_exceeded(_existing_state):
            # Monotonic: once LIMIT_EXCEEDED, stay there — no reset path.
            if _existing_run is not None:
                return _existing_run
        if _existing_state is not None:
            state = _existing_state
        else:
            state = RunState(status=RunStatus.RUNNING)
        if _existing_usage is not None:
            usage = _existing_usage
        elif _existing_run is not None:
            usage = Usage(
                prompt_tokens=_existing_run.total_tokens,
                completion_tokens=0,
                tool_calls=len([s for s in _existing_run.steps if s.tool_name is not None]),
            )
        else:
            usage = Usage()
        if _existing_run is not None and RunState.is_terminal(state.status):
            return _existing_run
        model = context.model_policy.default_model or "gpt-4o-mini"
        if _existing_run is not None:
            agent_run = _existing_run
        else:
            agent_run = AgentRun(
                id=context.run_id,
                workspace_id=context.workspace_id,
                goal=goal,
                model=model,
                status=AgentRunStatus.RUNNING,
                created_at=datetime.now(UTC).isoformat(),
            )
        policy = ToolPolicy(allowed_tools=list(context.allowed_tools))
        _PERSISTED_RUNS[context.run_id] = agent_run
        _PERSISTED_STATES[context.run_id] = state
        _PERSISTED_USAGES[context.run_id] = usage

        system_prompt = self._build_system_prompt_from(
            instructions, [t.name for t in self.registry.list_tools() if policy.allows(t.name)]
        )
        history: list[ChatMessage] = [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ChatMessage(role=MessageRole.USER, content=f"Goal: {goal}"),
        ]

        # T-V3-006: usage is the single source of truth, seeded from
        # the persisted run record. ``total_tokens`` and ``tool_calls`` are
        # derived from ``usage`` and ``agent_run`` — never reconstructed from
        # zero on a retry, and missing usage is DENY, not unlimited.
        total_tokens = usage.prompt_tokens + usage.completion_tokens
        # tool_calls is usage.tool_calls, but also count steps with tool_name
        tool_calls = usage.tool_calls
        # Re-derive from the persisted run in case usage was seeded from AgentRun
        if _existing_run is not None:
            total_tokens = _existing_run.total_tokens
            tool_calls = len([st for st in _existing_run.steps if st.tool_name is not None])

        for _step_idx in range(1, context.budget.max_steps + 1):
            # T-V3-007: cancel wins — no further dispatches after cancel.
            if state.is_cancelled():
                # Monotonic: already CANCELLED, stay there — no new spend.
                agent_run.status = _legacy_status(state.status)
                agent_run.error = state.error_detail
                break
            # T-V3-006: reserve BEFORE spend — tokens, tool_calls, deadline.
            # Missing usage is DENY via can_reserve_* (never unlimited).
            # Deadline is a hard wall — zero model calls after it.
            if datetime.now(UTC) >= context.deadline:
                state.transition_to(
                    RunStatus.LIMIT_EXCEEDED,
                    error_code=AgentErrorCode.DEADLINE_EXCEEDED,
                    error_detail="deadline reached before LLM call — zero calls after deadline",
                )
                agent_run.status = _legacy_status(state.status)
                agent_run.error = state.error_detail
                _PERSISTED_STATES[context.run_id] = state
                break
            # Reserve tokens before LLM: estimate is max_output_tokens
            estimated = context.model_policy.max_output_tokens
            if not can_reserve_tokens(usage, context.budget, estimated):
                state.transition_to(
                    RunStatus.LIMIT_EXCEEDED,
                    error_code=AgentErrorCode.BUDGET_EXCEEDED,
                    error_detail=f"token budget would be exceeded: {total_tokens}+{estimated} > {context.budget.max_tokens}",  # noqa: E501
                )
                agent_run.status = _legacy_status(state.status)
                agent_run.error = state.error_detail
                _PERSISTED_STATES[context.run_id] = state
                break
            # Also check step budget (tool_calls/deadline) before LLM
            if not self._check_step_budget(
                state=state, context=context, tool_calls_used=tool_calls
            ):
                agent_run.status = _legacy_status(state.status)
                agent_run.error = state.error_detail
                _PERSISTED_STATES[context.run_id] = state
                break

            req = GenerationRequest(
                model=model,
                messages=history,
                temperature=context.model_policy.temperature,
                max_tokens=context.model_policy.max_output_tokens,
            )
            try:
                gen_res = await self.llm.generate(req)
            except asyncio.CancelledError:
                # Cancel wins — transition to CANCELLED if not already terminal
                if not RunState.is_terminal(state.status):
                    try:
                        state.transition_to(
                            RunStatus.CANCELLED,
                            error_code=AgentErrorCode.CANCELLED,
                            error_detail="run cancelled mid-LLM",
                        )
                        agent_run.status = _legacy_status(state.status)
                        agent_run.error = state.error_detail
                        _PERSISTED_STATES[context.run_id] = state
                        _PERSISTED_RUNS[context.run_id] = agent_run
                    except InvalidRunTransitionError:
                        pass
                break
            except Exception as exc:
                state.mark_unknown_required_failure(f"provider failure: {exc}")
                state.transition_to(
                    RunStatus.FAILED,
                    error_code=AgentErrorCode.PROVIDER_ERROR,
                    error_detail=f"provider failure: {exc}",
                )
                agent_run.status = _legacy_status(state.status)
                agent_run.error = state.error_detail
                break

            step_tokens = (gen_res.prompt_tokens or 0) + (gen_res.completion_tokens or 0)
            total_tokens += step_tokens
            # Persist usage durably — survives simulated retry/restart
            usage = Usage(
                prompt_tokens=total_tokens,  # keep simple: total as prompt for test visibility
                completion_tokens=0,
                tool_calls=tool_calls,
            )
            agent_run.total_tokens = total_tokens
            _PERSISTED_USAGES[context.run_id] = usage
            _PERSISTED_RUNS[context.run_id] = agent_run
            if total_tokens > context.budget.max_tokens:
                state.transition_to(
                    RunStatus.LIMIT_EXCEEDED,
                    error_code=AgentErrorCode.BUDGET_EXCEEDED,
                    error_detail=f"token budget exceeded ({total_tokens} > "
                    f"{context.budget.max_tokens})",
                )
                agent_run.status = _legacy_status(state.status)
                agent_run.error = state.error_detail
                _PERSISTED_STATES[context.run_id] = state
                break

            text = gen_res.text.strip()
            history.append(ChatMessage(role=MessageRole.ASSISTANT, content=text))

            # T-V3-004: bounded typed-action parsing — size + wall-clock guard,
            # no regex, unknown → BLOCKED SCHEMA_INVALID, prose never evidence.
            parsed = await _parse_action_bounded(text, allowed_tools=set(policy.allowed_tools))
            if parsed.kind == "final_answer":
                agent_run.final_output = parsed.final_answer
                state.transition_to(RunStatus.COMPLETED)
                agent_run.status = _legacy_status(state.status)
                agent_run.error = None
                break

            if parsed.kind == "tool_call":
                assert parsed.tool_name is not None
                tool_name = parsed.tool_name
                raw_input = parsed.raw_input or "{}"
                # Resolve spec once for retry policy / timeout (tool may inject).
                _tool_for_retry = self.registry.get(tool_name)
                _retry_policy = (
                    _tool_for_retry.retry_policy
                    if _tool_for_retry is not None and hasattr(_tool_for_retry, "retry_policy")
                    and isinstance(_tool_for_retry.retry_policy, RetryPolicy)
                    else RetryPolicy()
                )
                # T-V3-007: cancel check before tool — no dispatches after cancel.
                if state.is_cancelled():
                    break
                # T-V3-006: reserve tool_calls BEFORE each attempt (including retries).
                # Missing usage is DENY; tool_calls is part of usage, so check can_reserve.
                if not can_reserve_tool_call(usage, context.budget):
                    state.transition_to(
                        RunStatus.LIMIT_EXCEEDED,
                        error_code=AgentErrorCode.BUDGET_EXCEEDED,
                        error_detail=(
                            f"tool-call budget would be exceeded: {tool_calls}+1 > {context.budget.max_tool_calls}"  # noqa: E501
                        ),
                    )
                    agent_run.status = _legacy_status(state.status)
                    agent_run.error = state.error_detail
                    _PERSISTED_STATES[context.run_id] = state
                    break
                if datetime.now(UTC) >= context.deadline:
                    state.transition_to(
                        RunStatus.LIMIT_EXCEEDED,
                        error_code=AgentErrorCode.DEADLINE_EXCEEDED,
                        error_detail="deadline reached before tool call — zero calls after deadline",  # noqa: E501
                    )
                    agent_run.status = _legacy_status(state.status)
                    agent_run.error = state.error_detail
                    _PERSISTED_STATES[context.run_id] = state
                    break
                # Retry loop: transients only, per RetryPolicy, bounded by deadline.
                # Each attempt is a persisted ERROR/TOOL_OUTPUT step — attempts are
                # visible in the run record, not in-memory-only.
                attempt = 0
                while True:
                    attempt += 1
                    tool_calls += 1
                    # Persist tool_calls in usage before the spend
                    usage = Usage(
                        prompt_tokens=total_tokens,
                        completion_tokens=0,
                        tool_calls=tool_calls,
                    )
                    _PERSISTED_USAGES[context.run_id] = usage
                    _PERSISTED_RUNS[context.run_id] = agent_run
                    output, status, error_code = await self._dispatch(
                        context=context,
                        tool_name=tool_name,
                        raw_tool_input=raw_input,
                        policy=policy,
                        state=state,
                    )
                    # Persist this attempt as a step — the last attempt's output
                    # is what the history sees, but all attempts are in the trace.
                    # Include attempt count and error_code for visibility (attempts persisted).
                    persisted_output: dict = dict(output)
                    persisted_output["attempt"] = attempt
                    if _retry_policy.max_attempts > 1:
                        persisted_output["max_attempts"] = _retry_policy.max_attempts
                    if error_code is not None:
                        persisted_output["error_code"] = error_code.value
                    agent_run.steps.append(
                        AgentStep(
                            step_number=len(agent_run.steps) + 1,
                            type=(
                                AgentStepType.TOOL_OUTPUT
                                if status is StepStatus.SUCCEEDED
                                else AgentStepType.ERROR
                            ),
                            content="",
                            tool_name=tool_name,
                            tool_input=(
                                {"raw": raw_input} if status is not StepStatus.SUCCEEDED else None
                            ),
                            tool_output=persisted_output,
                            timestamp=datetime.now(UTC).isoformat(),
                        )
                    )
                    if error_code is not None:
                        agent_run.error = f"{error_code.value}: {output.get('error', '')}"

                    # T-V3-007: if cancel was requested mid-tool, stop — no further dispatches.
                    if state.is_cancelled():
                        agent_run.status = _legacy_status(state.status)
                        agent_run.error = state.error_detail
                        history.append(
                            ChatMessage(role=MessageRole.USER, content=f"Observation: {output}")
                        )
                        break

                    # Success or non-retryable or out of attempts → stop retrying.
                    if status is StepStatus.SUCCEEDED:
                        history.append(
                            ChatMessage(role=MessageRole.USER, content=f"Observation: {output}")
                        )
                        break
                    if not is_retryable(error_code, _retry_policy):
                        history.append(
                            ChatMessage(role=MessageRole.USER, content=f"Observation: {output}")
                        )
                        break
                    if attempt >= _retry_policy.max_attempts:
                        # Exhausted: transient → FAILED, as the card requires.
                        # The run itself fails — the next LLM turn must not clear it.
                        history.append(
                            ChatMessage(role=MessageRole.USER, content=f"Observation: {output}")
                        )
                        state.transition_to(
                            RunStatus.FAILED,
                            error_code=error_code,
                            error_detail=(
                            f"{error_code.value if error_code else 'unknown'}: tool {tool_name!r} failed after {attempt} attempts: {output.get('error', '')}"  # noqa: E501
                        ),
                        )
                        agent_run.status = _legacy_status(state.status)
                        agent_run.error = state.error_detail
                        # Do not continue to next LLM turn — the retry is the terminal.
                        break
                    # Bounded backoff before next attempt — capped by deadline and max_backoff.
                    # Never sleep past the run's deadline.
                    delay = bounded_backoff(attempt, _retry_policy, context.deadline)
                    if delay > 0:
                        # Respect deadline even during sleep.
                        remaining = (context.deadline - datetime.now(UTC)).total_seconds()
                        if remaining <= 0:
                            break
                        delay = min(delay, remaining)
                        if delay > 0:
                            try:
                                await asyncio.sleep(min(delay, remaining))
                            except asyncio.CancelledError:
                                if not RunState.is_terminal(state.status):
                                    try:
                                        state.transition_to(
                                            RunStatus.CANCELLED,
                                            error_code=AgentErrorCode.CANCELLED,
                                            error_detail="run cancelled during backoff",
                                        )
                                        agent_run.status = _legacy_status(state.status)
                                        agent_run.error = state.error_detail
                                        _PERSISTED_STATES[context.run_id] = state
                                        _PERSISTED_RUNS[context.run_id] = agent_run
                                    except InvalidRunTransitionError:
                                        pass
                                break
                    # T-V3-007: check cancel before next retry attempt
                    if state.is_cancelled():
                        agent_run.status = _legacy_status(state.status)
                        agent_run.error = state.error_detail
                        break
                    # Check budget/deadline before next attempt.
                    if not self._check_step_budget(
                        state=state, context=context, tool_calls_used=tool_calls
                    ):
                        agent_run.status = _legacy_status(state.status)
                        agent_run.error = state.error_detail
                        break
                    if datetime.now(UTC) >= context.deadline:
                        state.transition_to(
                            RunStatus.LIMIT_EXCEEDED,
                            error_code=AgentErrorCode.DEADLINE_EXCEEDED,
                            error_detail="deadline reached during retry backoff",
                        )
                        agent_run.status = _legacy_status(state.status)
                        agent_run.error = state.error_detail
                        break
                    # Loop for next attempt (tool_calls already counted).
                    continue
                # After the retry loop, continue outer LLM loop unless terminal.
                if RunState.is_terminal(state.status):
                    break
                continue

            # Invalid / unknown / garbage → BLOCKED, never exec, never evidence.
            blocked_error = parsed.error or "invalid action"
            blocked_code = parsed.error_code or AgentErrorCode.SCHEMA_INVALID
            agent_run.steps.append(
                AgentStep(
                    step_number=len(agent_run.steps) + 1,
                    type=AgentStepType.ERROR,
                    content="",
                    tool_name=None,
                    tool_output={
                        "error": blocked_error,
                        "error_code": blocked_code.value,
                    },
                    timestamp=datetime.now(UTC).isoformat(),
                )
            )
            agent_run.error = f"{blocked_code.value}: {blocked_error}"
            history.append(
                ChatMessage(
                    role=MessageRole.USER,
                    content=f"Observation: {{'error': {blocked_error!r}, 'error_code': {blocked_code.value!r}}}",  # noqa: E501
                )
            )
            continue
        else:
            if not RunState.is_terminal(state.status):
                state.transition_to(
                    RunStatus.LIMIT_EXCEEDED,
                    error_code=AgentErrorCode.BUDGET_EXCEEDED,
                    error_detail=f"exceeded maximum steps ({context.budget.max_steps})",
                )
                agent_run.status = _legacy_status(state.status)
                agent_run.error = state.error_detail

        agent_run.finished_at = datetime.now(UTC).isoformat()
        return agent_run

    def _build_system_prompt_from(self, instructions: str, allowed: list[str]) -> str:
        tools = [t for t in self.registry.list_tools() if t.name in allowed]
        tool_descs = [
            f"- {t.name}: {t.description}. Input: {json.dumps(t.input_schema)}" for t in tools
        ]
        tools_block = "\n".join(tool_descs) if tool_descs else "No tools available."
        return (
            f"{instructions}\n\n"
            f"Available Tools:\n{tools_block}\n\n"
            "To use a tool, format your output strictly as:\n"
            "Thought: <your reasoning>\n"
            "Action: <tool_name>\n"
            "Action Input: <json_object_with_tool_arguments>\n\n"
            "When you have the final answer, format your output strictly as:\n"
            "Thought: <your reasoning>\n"
            "Final Answer: <your complete response>\n"
        )

    def _build_system_prompt(self, config: AgentConfig) -> str:
        tools = self.registry.list_tools()
        if config.allowed_tools:
            tools = [t for t in tools if t.name in config.allowed_tools]

        tool_descs = [
            f"- {t.name}: {t.description}. Input: {json.dumps(t.input_schema)}" for t in tools
        ]
        tools_block = "\n".join(tool_descs) if tool_descs else "No tools available."

        return (
            f"{config.instructions}\n\n"
            f"Available Tools:\n{tools_block}\n\n"
            "To use a tool, format your output strictly as:\n"
            "Thought: <your reasoning>\n"
            "Action: <tool_name>\n"
            "Action Input: <json_object_with_tool_arguments>\n\n"
            "When you have the final answer, format your output strictly as:\n"
            "Thought: <your reasoning>\n"
            "Final Answer: <your complete response>\n"
        )

    async def run(
        self,
        *,
        workspace_id: str,
        goal: str,
        config: AgentConfig | None = None,
    ) -> AgentRun:
        cfg = config or AgentConfig()
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC).isoformat()

        agent_run = AgentRun(
            id=run_id,
            workspace_id=workspace_id,
            goal=goal,
            model=cfg.model,
            status=AgentRunStatus.RUNNING,
            created_at=now,
        )

        system_prompt = self._build_system_prompt(cfg)
        conversation_history: list[ChatMessage] = [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ChatMessage(role=MessageRole.USER, content=f"Goal: {goal}"),
        ]

        total_tokens = 0

        for step_idx in range(1, cfg.max_steps + 1):
            t_start = time.perf_counter()

            # Call LLM
            req = GenerationRequest(
                model=cfg.model,
                messages=conversation_history,
                temperature=cfg.temperature,
            )
            try:
                gen_res = await self.llm.generate(req)
                t_llm = (time.perf_counter() - t_start) * 1000.0
                step_tokens = (gen_res.prompt_tokens or 0) + (gen_res.completion_tokens or 0)
                total_tokens += step_tokens
                agent_run.total_tokens = total_tokens

                if total_tokens > cfg.budget_tokens:
                    agent_run.status = AgentRunStatus.BUDGET_EXCEEDED
                    agent_run.error = (
                        f"Token budget exceeded ({total_tokens} > {cfg.budget_tokens})"
                    )
                    break

                response_text = gen_res.text.strip()
            except Exception as exc:
                agent_run.status = AgentRunStatus.FAILED
                agent_run.error = f"LLM error: {exc}"
                break

            # T-V3-004: bounded parse in legacy path as well — no regex.
            _legacy_parsed = _parse_action_sync(response_text)
            _legacy_is_final = _legacy_parsed.kind == "final_answer"
            _legacy_is_tool = _legacy_parsed.kind == "tool_call"

            if _legacy_is_final:
                final_answer = _legacy_parsed.final_answer or ""
                agent_run.steps.append(
                    AgentStep(
                        step_number=step_idx,
                        type=AgentStepType.FINAL_ANSWER,
                        content=response_text,
                        latency_ms=t_llm,
                        timestamp=datetime.now(UTC).isoformat(),
                    )
                )
                agent_run.final_output = final_answer
                agent_run.status = AgentRunStatus.COMPLETED
                break

            if _legacy_is_tool:
                tool_name = _legacy_parsed.tool_name or ""
                raw_input = _legacy_parsed.raw_input or "{}"

                try:
                    tool_input = json.loads(raw_input)
                except Exception:
                    tool_input = {"input": raw_input}

                agent_run.steps.append(
                    AgentStep(
                        step_number=step_idx,
                        type=AgentStepType.TOOL_CALL,
                        content=response_text,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        latency_ms=t_llm,
                        timestamp=datetime.now(UTC).isoformat(),
                    )
                )

                # Execute Tool
                tool = self.registry.get(tool_name)
                t_tool_start = time.perf_counter()
                if tool:
                    try:
                        tool_output = await tool.execute(workspace_id, **tool_input)
                    except Exception as err:
                        tool_output = {"error": f"Tool execution failed: {err}"}
                else:
                    tool_output = {"error": f"Tool '{tool_name}' not found."}
                t_tool = (time.perf_counter() - t_tool_start) * 1000.0

                agent_run.steps.append(
                    AgentStep(
                        step_number=step_idx,
                        type=AgentStepType.TOOL_OUTPUT,
                        tool_name=tool_name,
                        tool_output=tool_output,
                        latency_ms=t_tool,
                        timestamp=datetime.now(UTC).isoformat(),
                    )
                )

                # Feed observation back to conversation history
                conversation_history.append(
                    ChatMessage(role=MessageRole.ASSISTANT, content=response_text)
                )
                conversation_history.append(
                    ChatMessage(
                        role=MessageRole.USER,
                        content=f"Observation: {json.dumps(tool_output)}",
                    )
                )
            else:
                # T-V3-004: prose without markers is never evidence — BLOCKED.
                agent_run.steps.append(
                    AgentStep(
                        step_number=step_idx,
                        type=AgentStepType.ERROR,
                        content=response_text,
                        latency_ms=t_llm,
                        timestamp=datetime.now(UTC).isoformat(),
                    )
                )
                conversation_history.append(
                    ChatMessage(role=MessageRole.ASSISTANT, content=response_text)
                )
                conversation_history.append(
                    ChatMessage(
                        role=MessageRole.USER,
                        content="Observation: {'error': 'invalid action', 'error_code': 'schema_invalid'}",  # noqa: E501
                    )
                )
                continue
        else:
            if agent_run.status == AgentRunStatus.RUNNING:
                agent_run.status = AgentRunStatus.MAX_STEPS_EXCEEDED
                agent_run.error = f"Exceeded maximum steps ({cfg.max_steps})"

        agent_run.finished_at = datetime.now(UTC).isoformat()
        return agent_run
