"""Unit tests for Phase 11 Agent Runtime and Tool Framework."""

from collections.abc import AsyncIterator

import pytest
from agents import (
    AgentConfig,
    AgentRunStatus,
    AgentRuntime,
    AgentStepType,
    CalculatorTool,
    PythonSandboxTool,
    ToolRegistry,
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
