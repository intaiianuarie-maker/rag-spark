"""Unit tests for Phase 11 Agent Runtime and Tool Framework."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

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
    PythonSandboxTool,
    RunBudget,
    RunContext,
    RunState,
    RunStatus,
    StepEvent,
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
async def test_python_sandbox_tool_security() -> None:
    sandbox = PythonSandboxTool()

    # Valid execution
    valid_res = await sandbox.execute(
        "ws-1",
        code="data = [1, 2, 3, 4]\nresult = sum(x * 2 for x in data)",
    )
    assert valid_res.get("result") == "20"

    # Blocked imports
    bad_import = await sandbox.execute("ws-1", code="import os\nos.listdir('.')")
    assert "Security violation" in bad_import.get("error", "")

    bad_from = await sandbox.execute("ws-1", code="from subprocess import run\nrun('ls')")
    assert "Security violation" in bad_from.get("error", "")

    # Blocked built-ins
    bad_builtin = await sandbox.execute("ws-1", code="f = open('/etc/passwd')")
    assert "Security violation" in bad_builtin.get("error", "")


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
