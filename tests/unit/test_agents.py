"""Unit tests for Phase 11 Agent Runtime and Tool Framework."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from agents import (
    RESERVED_CONTEXT_FIELDS,
    AgentConfig,
    AgentErrorCode,
    AgentRunStatus,
    AgentRuntime,
    AgentStepType,
    CalculatorTool,
    ContextOverrideError,
    InvalidRunTransitionError,
    ModelPolicy,
    RunBudget,
    RunContext,
    RunState,
    RunStatus,
    StepEvent,
    Tool,
    ToolPolicy,
    ToolRegistry,
    ToolSpec,
    Usage,
    validate_tool_arguments,
)
from agents.contract_tests import (
    check_run_context_contract,
    check_run_state_contract,
    check_step_event_contract,
    check_tool_arguments_do_not_override_context,
    check_tool_policy_contract,
    check_tool_spec_contract,
)
from models.interfaces import (
    GenerationRequest,
    GenerationResult,
    LLMProvider,
)


class MockLLM(LLMProvider):
    """Deterministic mock LLM answering with pre-programmed turns."""

    provider_name = "mock"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.call_count = 0

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if self.call_count < len(self.responses):
            resp = self.responses[self.call_count]
        else:
            resp = "Thought: I am done.\nFinal Answer: Default response"
        self.call_count += 1
        return GenerationResult(
            text=resp,
            model=request.model,
            prompt_tokens=50,
            completion_tokens=25,
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        yield (await self.generate(request)).text


@pytest.mark.unit
async def test_calculator_tool() -> None:
    calc = CalculatorTool()
    res = await calc.execute("ws-1", expression="12 * 4 + 2")
    assert res.get("result") == 50

    res_prec = await calc.execute("ws-1", expression="-10 + 5 * 2")
    assert res_prec.get("result") == 0

    res_err = await calc.execute("ws-1", expression="10 / 0")
    assert "error" in res_err


@pytest.mark.unit
async def test_agent_runtime_react_multi_step() -> None:
    llm = MockLLM(
        [
            # Step 1: LLM chooses calculator tool
            "Thought: I need to calculate 25 * 4.\n"
            "Action: calculator\n"
            'Action Input: {"expression": "25 * 4"}',
            # Step 2: LLM receives observation and produces final answer
            "Thought: The calculation is complete.\n"
            "Final Answer: The result is 100.",
        ]
    )

    registry = ToolRegistry()
    registry.register(CalculatorTool())
    runtime = AgentRuntime(llm, registry)

    run = await runtime.run(
        workspace_id="ws-test",
        goal="Calculate 25 * 4",
        config=AgentConfig(max_steps=5),
    )

    assert run.status == AgentRunStatus.COMPLETED
    assert run.final_output == "The result is 100."
    assert len(run.steps) == 3

    # Step 1: Tool call
    assert run.steps[0].type == AgentStepType.TOOL_CALL
    assert run.steps[0].tool_name == "calculator"
    assert run.steps[0].tool_input == {"expression": "25 * 4"}

    # Step 2: Tool output
    assert run.steps[1].type == AgentStepType.TOOL_OUTPUT
    assert run.steps[1].tool_output == {"result": 100}

    # Step 3: Final answer
    assert run.steps[2].type == AgentStepType.FINAL_ANSWER
    assert "100" in run.steps[2].content


@pytest.mark.unit
async def test_agent_runtime_max_steps_exceeded() -> None:
    llm = MockLLM(
        [
            "Thought: Still calculating.\nAction: calculator\n"
            'Action Input: {"expression": "1 + 1"}',
            "Thought: Still calculating.\nAction: calculator\n"
            'Action Input: {"expression": "2 + 2"}',
        ]
    )
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    runtime = AgentRuntime(llm, registry)

    run = await runtime.run(
        workspace_id="ws-test",
        goal="Infinite loop task",
        config=AgentConfig(max_steps=2),
    )

    assert run.status == AgentRunStatus.MAX_STEPS_EXCEEDED
    assert "Exceeded maximum steps" in (run.error or "")


# --------------------------------------------------------------------------------------
# V3 (A0) typed execution contracts
# --------------------------------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agents"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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


@pytest.mark.unit
def test_run_context_fixture_validates() -> None:
    ctx = RunContext.model_validate(_load("run_context.example.json"))
    check_run_context_contract(ctx)


@pytest.mark.unit
def test_run_context_rejects_missing_identity() -> None:
    with pytest.raises(ValueError):
        RunContext(
            run_id="",
            workspace_id="ws-1",
            principal_id="u-1",
            authz_policy_revision="pol-1",
            model_policy=ModelPolicy(),
            budget=RunBudget(),
            deadline=datetime.now(UTC),
        )


@pytest.mark.unit
def test_run_context_rejects_naive_deadline() -> None:
    with pytest.raises(ValueError):
        RunContext(
            run_id="run-1",
            workspace_id="ws-1",
            principal_id="u-1",
            authz_policy_revision="pol-1",
            model_policy=ModelPolicy(),
            budget=RunBudget(),
            deadline=datetime.now(),  # naive — must be timezone-aware
        )


@pytest.mark.unit
def test_budget_bounds_are_validated() -> None:
    with pytest.raises(ValueError):
        RunBudget(max_steps=0)
    with pytest.raises(ValueError):
        RunBudget(max_tool_calls=-1)


@pytest.mark.unit
def test_tool_spec_fixture_validates() -> None:
    spec = ToolSpec.model_validate(_load("tool_spec.example.json"))
    check_tool_spec_contract(spec)


@pytest.mark.unit
def test_tool_spec_requires_strict_schemas() -> None:
    with pytest.raises(ValueError):
        ToolSpec(
            name="loose",
            input_schema={"type": "object", "properties": {}},  # no additionalProperties=false
        )


@pytest.mark.unit
def test_tool_spec_rejects_empty_name_or_version() -> None:
    base = _load("tool_spec.example.json")
    with pytest.raises(ValueError):
        ToolSpec.model_validate({**base, "name": ""})
    with pytest.raises(ValueError):
        ToolSpec.model_validate({**base, "version": ""})


@pytest.mark.unit
def test_empty_tool_policy_denies_everything() -> None:
    check_tool_policy_contract(ToolPolicy(allowed_tools=[]))


@pytest.mark.unit
def test_tool_policy_allows_only_listed() -> None:
    check_tool_policy_contract(ToolPolicy(allowed_tools=["calculator", "knowledge_read"]))


@pytest.mark.unit
def test_run_state_terminal_transitions_are_one_way() -> None:
    check_run_state_contract(RunState(status=RunStatus.QUEUED))


@pytest.mark.unit
def test_run_state_completed_blocked_by_unknown_failure() -> None:
    state = RunState(status=RunStatus.RUNNING)
    state.mark_unknown_required_failure("provider vanished")
    assert state.unknown_required_failure is True
    with pytest.raises(InvalidRunTransitionError):
        state.transition_to(RunStatus.COMPLETED)
    state.transition_to(RunStatus.FAILED, error_code=AgentErrorCode.UNKNOWN_REQUIRED)
    assert state.error_code is AgentErrorCode.UNKNOWN_REQUIRED


@pytest.mark.unit
def test_run_state_error_code_rejected_on_non_terminal() -> None:
    state = RunState(status=RunStatus.RUNNING)
    with pytest.raises(InvalidRunTransitionError):
        state.transition_to(RunStatus.WAITING_INPUT, error_code=AgentErrorCode.CANCELLED)


@pytest.mark.unit
def test_step_event_fixture_validates() -> None:
    event = StepEvent.model_validate(_load("step_event.example.json"))
    check_step_event_contract([event])


@pytest.mark.unit
def test_step_event_sequence_must_increase() -> None:
    base = _load("step_event.example.json")
    check_step_event_contract(
        [
            StepEvent.model_validate(base),
            StepEvent.model_validate({**base, "sequence": 2, "step_id": "step-0002"}),
        ]
    )
    with pytest.raises(ValueError):
        StepEvent.model_validate({**base, "sequence": 0})


@pytest.mark.unit
def test_step_event_failed_requires_error_code() -> None:
    base = _load("step_event.example.json")
    with pytest.raises(ValueError):
        StepEvent.model_validate({**base, "status": "failed", "error_code": None})


@pytest.mark.unit
def test_tool_arguments_cannot_override_context() -> None:
    check_tool_arguments_do_not_override_context({"expression": "1 + 1"})
    for field in sorted(RESERVED_CONTEXT_FIELDS):
        with pytest.raises(ContextOverrideError):
            validate_tool_arguments({field: "evil"})


@pytest.mark.unit
def test_usage_cost_is_optional() -> None:
    usage = Usage(prompt_tokens=10, completion_tokens=5, tool_calls=1)
    assert usage.cost_usd is None


# --------------------------------------------------------------------------------------
# V3 (A1) enforced dispatch — rew1 B2/B3/B4 fail-before-fix tests
# --------------------------------------------------------------------------------------


class _ScriptedLLM(LLMProvider):
    """LLM that emits a fixed action then a final answer."""

    provider_name = "scripted"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        text = (
            self.responses[self.calls]
            if self.calls < len(self.responses)
            else "Final Answer: done"
        )
        self.calls += 1
        return GenerationResult(
            text=text, model=request.model, prompt_tokens=10, completion_tokens=5
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        yield (await self.generate(request)).text


def _enforced_context(
    *,
    allowed_tools: list[str] | None = None,
    budget: RunBudget | None = None,
    deadline: datetime | None = None,
) -> RunContext:
    """Build a RunContext with overridable policy/budget/deadline for tests."""
    return RunContext(
        run_id=f"run-{uuid.uuid4().hex[:8]}",
        workspace_id="ws-1",
        principal_id="user-1",
        authz_policy_revision="pol-1",
        model_policy=ModelPolicy(default_model="test-model"),
        budget=budget or RunBudget(),
        deadline=deadline or (datetime.now(UTC) + timedelta(seconds=60)),
        allowed_tools=allowed_tools if allowed_tools is not None else ["calculator"],
    )


def test_enforced_dispatch_denies_unlisted_tool() -> None:
    """B2: a tool outside the allowlist must be BLOCKED, not executed."""
    registry = ToolRegistry()
    calls = []

    class _Recording(Tool):
        name = "hidden_tool"
        description = "hidden"
        input_schema: ClassVar[dict] = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "additionalProperties": False,
        }

        async def execute(self, workspace_id: str, **kwargs) -> object:
            calls.append(kwargs)
            return {"ok": True}

    registry.register(_Recording())
    llm = _ScriptedLLM(
        ['Thought: use hidden\nAction: hidden_tool\nAction Input: {"x": 1}', "Final Answer: ok"]
    )
    runtime = AgentRuntime(llm, registry)
    run = asyncio.run(runtime.run_enforced(context=_enforced_context(), goal="try"))
    assert calls == [], "BLOCKED tool must not execute"
    assert run.status == AgentRunStatus.COMPLETED
    assert run.error is not None
    assert "tool_not_allowed" in run.error
    assert any(s.type == AgentStepType.ERROR for s in run.steps)


def test_enforced_dispatch_rejects_reserved_context_override() -> None:
    """B3: tool arguments must not be able to override the run context."""
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    llm = _ScriptedLLM(
        [
            'Thought: escalate\nAction: calculator\n'
            'Action Input: {"expression": "1+1", "workspace_id": "ws-victim"}',
            "Final Answer: ok",
        ]
    )
    runtime = AgentRuntime(llm, registry)

    run = asyncio.run(runtime.run_enforced(context=_enforced_context(), goal="try"))
    assert "tool_not_allowed" in (run.error or "")
    assert not any(
        s.tool_output == {"result": 2} for s in run.steps
    ), "override attempt must not execute the tool"


def test_enforced_dispatch_validates_strict_schema() -> None:
    """B3: extra/missing properties must be rejected by the ToolSpec schema."""

    class _StrictTool(Tool):
        name = "strict"
        description = "strict"
        input_schema: ClassVar[dict] = {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "required": ["a"],
            "additionalProperties": False,
        }

        async def execute(self, workspace_id: str, **kwargs) -> object:
            return {"echo": kwargs}

    registry = ToolRegistry()
    registry.register(_StrictTool())
    llm = _ScriptedLLM(
        [
            'Thought: bad args\nAction: strict\nAction Input: {"a": 1, "b": 2}',
            "Final Answer: ok",
        ]
    )
    runtime = AgentRuntime(llm, registry)

    run = asyncio.run(
        runtime.run_enforced(
            context=_enforced_context(allowed_tools=["strict"]), goal="try"
        )
    )
    assert "tool_schema_invalid" in (run.error or "")


def test_enforced_dispatch_enforces_tool_call_budget() -> None:
    """B4: max_tool_calls must stop the run with a limit state."""
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    # Every step asks for a tool; budget is 2 tool calls.
    llm = _ScriptedLLM(
        [
            f'Thought: x{idx}\nAction: calculator\nAction Input: {{"expression": "1+1"}}'
            for idx in range(10)
        ]
    )
    runtime = AgentRuntime(llm, registry)

    run = asyncio.run(
        runtime.run_enforced(
            context=_enforced_context(
                budget=RunBudget(max_steps=20, max_tool_calls=2, max_tokens=100000)
            ),
            goal="loop",
        )
    )
    assert run.status == AgentRunStatus.MAX_STEPS_EXCEEDED
    assert "budget" in (run.error or "")


def test_enforced_dispatch_checks_deadline_before_spend() -> None:
    """B4: a past deadline stops the run before any model call."""

    class _CountingLLM(LLMProvider):
        provider_name = "counting"
        calls = 0

        async def generate(self, request: GenerationRequest) -> GenerationResult:
            type(self).calls += 1
            return GenerationResult(text="Final Answer: x", model=request.model)

        async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
            yield "x"

    registry = ToolRegistry()
    runtime = AgentRuntime(_CountingLLM(), registry)

    before = _CountingLLM.calls
    run = asyncio.run(
        runtime.run_enforced(
            context=_enforced_context(deadline=datetime.now(UTC) - timedelta(seconds=5)),
            goal="expired",
        )
    )
    assert _CountingLLM.calls == before, "no model call after deadline"
    assert run.status == AgentRunStatus.MAX_STEPS_EXCEEDED
    assert "deadline" in (run.error or "")


def test_enforced_dispatch_enforces_token_budget() -> None:
    """B4: token budget is enforced across steps."""

    class _BigLLM(LLMProvider):
        provider_name = "big"

        async def generate(self, request: GenerationRequest) -> GenerationResult:
            return GenerationResult(
                text="Final Answer: done", model=request.model, prompt_tokens=900,
                completion_tokens=900,
            )

        async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
            yield "done"

    runtime = AgentRuntime(_BigLLM(), ToolRegistry())

    run = asyncio.run(
        runtime.run_enforced(
            context=_enforced_context(budget=RunBudget(max_tokens=1000)), goal="tokens"
        )
    )
    assert run.status == AgentRunStatus.MAX_STEPS_EXCEEDED
    assert "token budget" in (run.error or "")


def test_enforced_dispatch_enforces_output_size_bound() -> None:
    """B3: oversized tool output is rejected, not returned to the model."""

    class _VerboseTool(Tool):
        name = "verbose"
        description = "verbose"
        input_schema: ClassVar[dict] = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "additionalProperties": False,
        }

        async def execute(self, workspace_id: str, **kwargs) -> object:
            return {"blob": "A" * 100000}

    registry = ToolRegistry()
    registry.register(_VerboseTool())
    llm = _ScriptedLLM(
        ['Thought: big\nAction: verbose\nAction Input: {"x": 1}', "Final Answer: ok"]
    )
    runtime = AgentRuntime(llm, registry)

    run = asyncio.run(
        runtime.run_enforced(
            context=_enforced_context(allowed_tools=["verbose"]), goal="try"
        )
    )
    assert "tool_output_too_large" in (run.error or "")
