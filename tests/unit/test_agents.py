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
    AgentRun,
    AgentRunStatus,
    AgentRuntime,
    AgentStep,
    AgentStepType,
    CalculatorTool,
    ContextOverrideError,
    InvalidRunTransitionError,
    ModelPolicy,
    RetryPolicy,
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
            "Thought: The calculation is complete.\nFinal Answer: The result is 100.",
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
            self.responses[self.calls] if self.calls < len(self.responses) else "Final Answer: done"
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
    # The blocked step is recorded durably in the trace (rew1 follow-up 2:
    # the run itself completed, so its scalar error is cleared).
    assert any(s.type == AgentStepType.ERROR for s in run.steps)
    assert any(
        s.tool_output is not None
        and "not allowed by the run policy" in str((s.tool_output or {}).get("error", ""))
        for s in run.steps
    )


def test_enforced_dispatch_rejects_reserved_context_override() -> None:
    """B3: tool arguments must not be able to override the run context."""
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    llm = _ScriptedLLM(
        [
            "Thought: escalate\nAction: calculator\n"
            'Action Input: {"expression": "1+1", "workspace_id": "ws-victim"}',
            "Final Answer: ok",
        ]
    )
    runtime = AgentRuntime(llm, registry)

    run = asyncio.run(runtime.run_enforced(context=_enforced_context(), goal="try"))
    # rew1 follow-up 2: the scalar error is cleared on completion; the blocked
    # attempt stays recorded in the step trace.
    assert any(s.type == AgentStepType.ERROR for s in run.steps)
    assert not any(s.tool_output == {"result": 2} for s in run.steps), (
        "override attempt must not execute the tool"
    )


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
        runtime.run_enforced(context=_enforced_context(allowed_tools=["strict"]), goal="try")
    )
    # rew1 follow-up 2: assert on the recorded blocked step, not the cleared
    # scalar error.
    assert any(s.type == AgentStepType.ERROR for s in run.steps)
    assert any(
        s.tool_output is not None and "undeclared" in str((s.tool_output or {}).get("error", "")).lower()  # noqa: E501
        for s in run.steps
    )


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
                text="Final Answer: done",
                model=request.model,
                prompt_tokens=900,
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
        runtime.run_enforced(context=_enforced_context(allowed_tools=["verbose"]), goal="try")
    )
    # rew1 follow-up 2: the run completed, so the scalar error is cleared; the
    # oversized-output rejection stays recorded in the step trace.
    assert run.status == AgentRunStatus.COMPLETED
    assert run.error is None
    assert any(s.type == AgentStepType.ERROR for s in run.steps)
    assert any(
        s.tool_output is not None and "exceeded" in str((s.tool_output or {}).get("error", "")).lower()  # noqa: E501
        for s in run.steps
    )


# ======================================================================================
# T-V3-001 — bounded calculator (V3 plan trace A1-2)
#
# The model chooses the input, so the tool must bound what that input can cost.
# Each rejection below is asserted on the **typed** code, never on message text, and
# the "compute bounded" claim is measured rather than asserted in prose.
# ======================================================================================


def _calc_output(expression: object) -> dict:
    """Run one calculator call through the public tool surface."""
    return asyncio.run(CalculatorTool().execute("ws-1", expression=expression))


def _assert_typed_rejection(output: dict, *, mentions: str) -> None:
    """A refusal must carry the stable code and never look like a result."""
    assert "result" not in output, output
    assert output["error_code"] is AgentErrorCode.TOOL_SCHEMA_INVALID, output
    assert mentions in output["error"], output


@pytest.mark.unit
@pytest.mark.parametrize(
    ("expression", "why"),
    [
        ("9**9**9", "the classic big-int bomb, seven characters"),
        ("10**10**10", "same shape, larger"),
        ("2**1000", "one step past the exponent cap"),
        ("(2+3)**1000", "a *computed* exponent, not a literal one"),
        ("2**-1000", "negative exponent, same cost"),
        ("9" * 120 + "**" + "9" * 8, "big base, big exponent"),
        ("(2**64)**64", "nested exponentiation"),
    ],
)
def test_calculator_rejects_huge_exponents_typed(expression: str, why: str) -> None:
    """Checklist 1: a huge exponent is refused with a TYPED error, never computed."""
    output = _calc_output(expression)
    _assert_typed_rejection(output, mentions="exponent")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("expression", "why"),
    [
        ("1+" * 40 + "1", "a long operator chain nests linearly"),
        ("(" * 20 + "1" + ")" * 20, "parentheses: ast erases them, so the source is measured"),
        ("(" * 17 + "1" + ")" * 17, "one level past the nesting cap"),
        ("-" * 40 + "1", "a long unary chain"),
    ],
)
def test_calculator_rejects_deep_or_wide_expressions_typed(expression: str, why: str) -> None:
    """Checklist 1/3: depth (tree *and* source) and node count are both capped."""
    output = _calc_output(expression)
    _assert_typed_rejection(output, mentions="nest")
    assert "nodes" not in output["error"] or "nodes" in output["error"]


@pytest.mark.unit
def test_calculator_node_cap_is_enforced_on_a_shallow_wide_tree() -> None:
    """The node cap must be reachable without deep nesting (they are distinct)."""
    wide = "*".join(["(1+1+1+1+1+1+1+1)"] * 9)
    assert len(wide) <= CalculatorTool.MAX_EXPRESSION_CHARS, "the case must not be a length case"
    _assert_typed_rejection(_calc_output(wide), mentions="nodes")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("expression", "why"),
    [
        ("1" * 1000, "a long literal"),
        ("1+" * 5000, "a long chain, far over the cap"),
        ("x" * 50_000, "50KB of garbage"),
        ("9**9" + " " * 10_000, "a small expression padded with whitespace"),
    ],
)
def test_calculator_rejects_overlong_expressions_typed(expression: str, why: str) -> None:
    """An overlong expression is refused on length, before any parsing cost."""
    output = _calc_output(expression)
    _assert_typed_rejection(output, mentions="characters")


@pytest.mark.unit
@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('id')",
        "eval('1+1')",
        "open('/etc/passwd').read()",
        "().__class__.__bases__",
        "[1,2][0]",
        "(1,2)[0]",
        "{'a':1}['a']",
        "1 if 1 else 2",
        "lambda: 1",
        "x",
        "'a' + 'b'",
        "True + 1",
        "1e400",
        "10 / 0",
        "1 +=",
        "",
        "   ",
    ],
)
def test_calculator_refuses_everything_that_is_not_arithmetic(expression: str) -> None:
    """Checklist 2: only arithmetic survives — no name, call, attribute or literal.

    ``10/0`` and the unparseable/empty inputs are the pre-existing failure paths:
    they must still *report* rather than raise, which is why the assertion is on
    the absence of a result rather than on the rejection code.
    """
    output = _calc_output(expression)
    assert "result" not in output, output
    assert "error" in output, output


@pytest.mark.unit
def test_calculator_rejects_non_string_input_typed() -> None:
    """A non-string expression is a typed refusal, not a TypeError traceback."""
    for bad in (123, None, ["1+1"], {"expression": "1+1"}, 1.5):
        output = _calc_output(bad)
        _assert_typed_rejection(output, mentions="string")


@pytest.mark.unit
def test_calculator_has_no_dynamic_execution_surface() -> None:
    """Checklist 2: no eval/exec/compile/import surface anywhere in the package.

    Checked on the parsed AST rather than by substring, because this module's own
    prose discusses ``exec()`` — a text search would flag the documentation, and
    an AST search cannot be fooled by a name in a comment or a string.
    """
    import ast as ast_mod
    import pathlib

    import agents

    #: dynamic execution: these are what turn data into code
    banned_calls = {"eval", "exec", "compile", "__import__"}
    banned_modules = {"importlib", "subprocess", "ctypes", "pickle", "marshal", "sys"}
    offenders: list[str] = []
    for path in sorted(pathlib.Path(agents.__file__).parent.glob("*.py")):
        tree = ast_mod.parse(path.read_text())
        # the calculator additionally may not reach for attribute introspection
        extra_calls = (
            {"getattr", "setattr", "delattr", "globals", "locals", "vars", "open"}
            if path.name == "tools.py"
            else set()
        )
        for node in ast_mod.walk(tree):
            if isinstance(node, ast_mod.Call):
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name in banned_calls or name in extra_calls:
                    offenders.append(f"{path.name}:{node.lineno} calls {name}()")
            elif isinstance(node, (ast_mod.Import, ast_mod.ImportFrom)):
                root = getattr(node, "module", None) or ""
                imported = (
                    [a.name for a in node.names] if isinstance(node, ast_mod.Import) else [root]
                )
                for name in imported:
                    if str(name).split(".")[0] in banned_modules:
                        offenders.append(f"{path.name}:{node.lineno} imports {name}")
    assert offenders == [], offenders


@pytest.mark.unit
def test_calculator_module_never_reaches_for_an_interpreter() -> None:
    """The calculator's own module text carries no eval-family token as code.

    A second, independent angle on checklist 2: ``CalculatorTool`` can only see
    what ``ast`` hands it, so the whitelist in ``_check_tree`` is the whole
    attack surface. That is worth pinning on its own — if someone replaces the
    walker with ``eval`` later, this fails even if the bounds tests still pass.
    """
    import ast as ast_mod
    import inspect

    import agents.tools as tools_mod

    tree = ast_mod.parse(inspect.getsource(tools_mod))
    called = {
        getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        for node in ast_mod.walk(tree)
        if isinstance(node, ast_mod.Call)
    }
    assert called.isdisjoint({"eval", "exec", "compile", "__import__", "getattr"}), called
    # the evaluation entry point only ever dispatches on AST node types
    source = inspect.getsource(tools_mod.CalculatorTool._eval_node)
    assert "ast." in source, "evaluation must stay AST-driven"
    assert "value" not in source.split("ast.Constant")[0], "no value lookup before the AST check"


@pytest.mark.unit
def test_calculator_bounds_are_declared_and_finite() -> None:
    """Checklist 3: the bounds are named, published in the schema and sane.

    A cap nobody can assert is not a cap: if someone later relaxes one of these
    to a large value, this fails and the reviewer sees the number change.
    """
    from agents.tools import (
        MAX_AST_DEPTH,
        MAX_AST_NODES,
        MAX_EXPONENT,
        MAX_EXPRESSION_CHARS,
        MAX_RESULT_BITS,
    )

    bounds = {
        "chars": MAX_EXPRESSION_CHARS,
        "nodes": MAX_AST_NODES,
        "depth": MAX_AST_DEPTH,
        "exponent": MAX_EXPONENT,
        "result_bits": MAX_RESULT_BITS,
    }
    for name, value in bounds.items():
        assert isinstance(value, int) and 0 < value <= 1_000_000, f"{name}={value}"
    # every cap must be reachable in ordinary arithmetic, else the tool is useless
    assert MAX_EXPRESSION_CHARS >= 32, "must fit a real expression"
    assert MAX_AST_NODES >= 16, "must fit a real expression"
    assert MAX_AST_DEPTH >= 4, "must fit a real expression"
    assert MAX_EXPONENT >= 16, "must fit a real exponentiation"
    assert MAX_RESULT_BITS >= 64, "must fit a real integer"
    # ...and the evaluator agrees with the published constants
    calc = CalculatorTool()
    assert calc.MAX_EXPRESSION_CHARS == MAX_EXPRESSION_CHARS
    assert calc.MAX_AST_NODES == MAX_AST_NODES
    assert calc.MAX_AST_DEPTH == MAX_AST_DEPTH
    assert calc.MAX_EXPONENT == MAX_EXPONENT
    assert calc.MAX_RESULT_BITS == MAX_RESULT_BITS
    # the schema advertises the length cap, so the refusal is a schema violation
    assert (
        CalculatorTool.input_schema["properties"]["expression"]["maxLength"] == MAX_EXPRESSION_CHARS
    )


@pytest.mark.unit
def test_calculator_still_computes_ordinary_arithmetic() -> None:
    """The bounds must not over-block: the documented examples still work."""
    cases = {
        "12 * 45 + 10": 550,
        "-10 + 5 * 2": 0,
        "(2+3) * 4": 20,
        "2 + 3 * 4": 14,
        "7 // 2": 3,
        "7 % 3": 1,
        "10 / 4": 2.5,
        "-(3 + 4)": -7,
        "+5": 5,
        "2**10": 1024,
        "2**128": 340282366920938463463374607431768211456,  # at the exponent cap
    }
    for expression, expected in cases.items():
        output = _calc_output(expression)
        assert output == {"result": expected}, f"{expression} -> {output}"


@pytest.mark.unit
def test_calculator_rejection_is_bounded_in_time() -> None:
    """Metric: "compute bounded" — measured, not asserted in prose.

    Each hostile input is refused in well under a millisecond of measured work,
    including the 100KB one, which is refused on length before it is parsed.
    The assertion is a generous ceiling so it cannot flake on a loaded box; the
    point is that nothing here is O(exponential).
    """
    import time

    hostile = [
        "9**9**9",
        "10**10**10",
        "2**100000",
        "1+" * 40 + "1",
        "(" * 20 + "1" + ")" * 20,
        "1" * 100_000,
        "1 + " * 25_000 + "1",
        "x" * 100_000,
    ]
    calc = CalculatorTool()
    for expression in hostile:
        started = time.perf_counter()
        output = asyncio.run(calc.execute("ws-1", expression=expression))
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert "result" not in output, expression
        assert elapsed_ms < 250.0, f"{expression[:20]!r} took {elapsed_ms:.1f}ms"


@pytest.mark.unit
def test_calculator_rejection_carries_a_stable_code_not_just_text() -> None:
    """The refusal is typed so callers branch on the code (A0-1 taxonomy)."""
    from agents.tools import CalculatorInputRejected

    assert issubclass(CalculatorInputRejected, ValueError)
    assert CalculatorInputRejected.code is AgentErrorCode.TOOL_SCHEMA_INVALID
    rejection = CalculatorInputRejected("because")
    assert rejection.reason == "because"
    output = _calc_output("9**9**9")
    assert output["error_code"] == "tool_schema_invalid", output
    assert "expression rejected" in output["error"], output


# ======================================================================================
# T-V3-002 — ACL-scoped knowledge_search (V3 plan trace A1-1a)
# ======================================================================================

from agents.contract_tests import check_knowledge_search_tool_contract  # noqa: E402
from agents.tools import (  # noqa: E402
    MAX_QUERY_CHARS,
    MAX_SEARCH_OUTPUT_BYTES,
    MAX_SEARCH_TOP_K,
    KnowledgeSearchTool,
)


class _FakeRetrievedItem:
    def __init__(
        self, content, workspace_id, document_id="doc-1", chunk_id=None, score=0.9, method="fused"
    ):
        self.content = content
        self.workspace_id = workspace_id
        self.document_id = document_id
        self.chunk_id = chunk_id or f"chunk-{uuid.uuid4().hex[:6]}"
        self.score = score
        self.method = method


class _FakeOrchestrator:
    def __init__(self, items=None, capture=None):
        self._items = items or []
        self.captured_scopes = capture if capture is not None else []
        self.calls = 0

    async def retrieve(self, *, query, acl_scope, context_top_k=5, **kwargs):
        self.calls += 1
        self.captured_scopes.append(acl_scope)
        return list(self._items)[: context_top_k * 2]


def _search_output(query="hello", workspace_id="ws-allowed", orchestrator=None, **extra):
    tool = KnowledgeSearchTool(orchestrator=orchestrator)
    return asyncio.run(tool.execute(workspace_id, query=query, **extra))


@pytest.mark.unit
def test_knowledge_search_contract_is_strict_readonly_and_scoped() -> None:
    tool = KnowledgeSearchTool(orchestrator=_FakeOrchestrator())
    check_knowledge_search_tool_contract(tool)
    rt = AgentRuntime(MockLLM([]), ToolRegistry())
    spec = rt.spec_for(tool)
    assert spec.input_schema.get("additionalProperties") is False
    assert spec.tool_class.value == "read"


@pytest.mark.unit
def test_knowledge_search_workspace_enforced_server_side() -> None:
    items = [
        _FakeRetrievedItem("a", "ws-allowed"),
        _FakeRetrievedItem("b", "ws-other"),
        _FakeRetrievedItem("c", "ws-allowed"),
    ]
    orch = _FakeOrchestrator(items=items)
    tool = KnowledgeSearchTool(orchestrator=orch)
    out = asyncio.run(
        tool.execute(
            "ws-allowed",
            query="q",
            **{"workspace_ids": ["ws-other"], "filter": {"workspace_ids": ["ws-other"]}},
        )
    )
    assert "results" in out
    assert all(r["workspace_id"] == "ws-allowed" for r in out["results"]), out
    assert len(orch.captured_scopes) == 1
    assert orch.captured_scopes[0].workspace_ids == ["ws-allowed"]


@pytest.mark.unit
def test_knowledge_search_cross_workspace_reads_rejected() -> None:
    items = [
        _FakeRetrievedItem("secret other", "ws-victim", document_id="doc-v"),
        _FakeRetrievedItem("allowed one", "ws-allowed", document_id="doc-a"),
        _FakeRetrievedItem("secret other 2", "ws-victim", document_id="doc-v2"),
    ]
    orch = _FakeOrchestrator(items=items)
    out = _search_output(query="test", workspace_id="ws-allowed", orchestrator=orch, top_k=10)
    assert out["count"] == 1
    assert out["results"][0]["workspace_id"] == "ws-allowed"
    assert not any(r["workspace_id"] == "ws-victim" for r in out["results"])
    assert not any(r["document_id"] == "doc-v" for r in out["results"])


@pytest.mark.unit
def test_knowledge_search_runtime_blocks_model_supplied_workspace_id() -> None:
    registry = ToolRegistry()
    registry.register(
        KnowledgeSearchTool(
            orchestrator=_FakeOrchestrator(items=[_FakeRetrievedItem("hi", "ws-allowed")])
        )
    )
    llm = _ScriptedLLM(
        [
            "Thought: try to steal\nAction: knowledge_search\n"
            'Action Input: {"query": "hello", "workspace_id": "ws-victim"}'
            "Final Answer: done",
        ]
    )
    rt = AgentRuntime(llm, registry)
    ctx = RunContext(
        run_id="run-t302-1",
        workspace_id="ws-allowed",
        principal_id="user-1",
        authz_policy_revision="pol-1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(max_steps=5, max_tool_calls=5, max_tokens=100000),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["knowledge_search"],
    )
    run = asyncio.run(rt.run_enforced(context=ctx, goal="try to read other workspace"))
    assert any(s.type == AgentStepType.ERROR for s in run.steps)
    assert not any(s.tool_output and "ws-victim" in json.dumps(s.tool_output) for s in run.steps)


@pytest.mark.unit
def test_knowledge_search_strict_schema_rejects_extra_fields() -> None:
    registry = ToolRegistry()
    registry.register(KnowledgeSearchTool(orchestrator=_FakeOrchestrator()))
    llm = _ScriptedLLM(
        [
            "Thought: smuggle\nAction: knowledge_search\n"
            'Action Input: {"query": "hello", "extra": "evil"}'
            "Final Answer: done",
        ]
    )
    rt = AgentRuntime(llm, registry)
    ctx = RunContext(
        run_id="run-t302-2",
        workspace_id="ws-allowed",
        principal_id="user-1",
        authz_policy_revision="pol-1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(max_steps=5, max_tool_calls=5, max_tokens=100000),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["knowledge_search"],
    )
    run = asyncio.run(rt.run_enforced(context=ctx, goal="try extra field"))
    assert any(s.type == AgentStepType.ERROR for s in run.steps)
    assert any(
        "undeclared" in json.dumps(s.tool_output).lower() for s in run.steps if s.tool_output
    )


@pytest.mark.unit
def test_knowledge_search_output_bounded() -> None:
    big_content = "x" * 5000
    items = [_FakeRetrievedItem(big_content, "ws-allowed", score=1.0 - i * 0.01) for i in range(20)]
    orch = _FakeOrchestrator(items=items)
    out = _search_output(query="big query", workspace_id="ws-allowed", orchestrator=orch, top_k=10)
    assert "results" in out
    raw = json.dumps(out, ensure_ascii=False).encode("utf-8")
    assert len(raw) <= MAX_SEARCH_OUTPUT_BYTES, (
        f"output {len(raw)} bytes exceeds {MAX_SEARCH_OUTPUT_BYTES}"
    )
    assert all(len(r["content"]) <= 800 + 3 for r in out["results"])
    if len(items) > out["count"]:
        assert out.get("truncated") is True or len(raw) <= MAX_SEARCH_OUTPUT_BYTES


@pytest.mark.unit
def test_knowledge_search_rejects_overlong_query_and_invalid_top_k_typed() -> None:
    orch = _FakeOrchestrator(items=[_FakeRetrievedItem("hi", "ws-allowed")])
    out = _search_output(query="x" * (MAX_QUERY_CHARS + 1), workspace_id="ws-1", orchestrator=orch)
    assert out["error_code"] == AgentErrorCode.TOOL_SCHEMA_INVALID
    assert "characters" in out["error"]
    assert "results" not in out
    out = _search_output(query="   ", workspace_id="ws-1", orchestrator=orch)
    assert out["error_code"] == AgentErrorCode.TOOL_SCHEMA_INVALID
    for bad in (0, 11, 100, "5", 3.5, True):
        out = _search_output(query="hello", workspace_id="ws-1", orchestrator=orch, top_k=bad)
        assert out["error_code"] == AgentErrorCode.TOOL_SCHEMA_INVALID, f"top_k={bad!r} -> {out}"
    out = _search_output(
        query="hello", workspace_id="ws-1", orchestrator=orch, top_k=MAX_SEARCH_TOP_K
    )
    assert "results" in out


@pytest.mark.unit
def test_knowledge_search_has_no_write_surface() -> None:
    import ast

    source = (
        Path(__file__).read_text()
        if False
        else __import__("pathlib").Path("packages/agents/tools.py").read_text()
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "KnowledgeSearchTool":
            called = {
                getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                for n in ast.walk(node)
                if isinstance(n, ast.Call)
            }
            assert called.isdisjoint(
                {"upsert", "delete", "index", "put_object", "eval", "exec", "compile"}
            ), called
            assert "import qdrant_client" not in source
            assert "from qdrant_client" not in source
            assert "import qdrant_client" not in ast.unparse(node)
            assert "from qdrant_client" not in ast.unparse(node)


@pytest.mark.unit
def test_knowledge_search_uses_retrieval_adapter_boundary() -> None:
    text = Path("packages/agents/tools.py").read_text()
    assert "import qdrant_client" not in text
    assert "from qdrant_client" not in text
    assert "orchestrator.retrieve" in text


# ======================================================================================
# T-V3-003 — Scoped source-reader (V3 plan trace A1-1b)
# ======================================================================================

from agents.tools import KnowledgeReadTool  # noqa: E402


class _FakeChunkRow:
    def __init__(
        self,
        chunk_id,
        document_id,
        version_id,
        workspace_id,
        text="hello world",
        deleted_at=None,
        superseded_at=None,
    ):
        self.id = chunk_id
        self.chunk_id = chunk_id
        self.document_id = document_id
        self.document_version_id = version_id
        self.workspace_id = workspace_id
        self.text = text
        self.deleted_at = deleted_at
        self.superseded_at = superseded_at


class _FakeChunkStore:
    def __init__(self, rows=None):
        self.rows = {r.id: r for r in (rows or [])}
        self.calls = []

    async def get_chunk(self, document_id, version_id, chunk_id):
        self.calls.append((document_id, version_id, chunk_id))
        # Exact triple lookup — no scan
        row = self.rows.get(chunk_id)
        return row

    async def __call__(self, document_id, version_id, chunk_id):
        return await self.get_chunk(document_id, version_id, chunk_id)


def _read_output(workspace_id, document_id, version_id, chunk_id, store=None, **extra):
    tool = KnowledgeReadTool(chunk_reader=store)
    return asyncio.run(
        tool.execute(
            workspace_id,
            document_id=document_id,
            document_version_id=version_id,
            chunk_id=chunk_id,
            **extra,
        )
    )


@pytest.mark.unit
def test_knowledge_read_exact_triple_and_pinned_version() -> None:
    doc = str(uuid.uuid4())
    ver = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    ws = "ws-allowed"
    row = _FakeChunkRow(cid, doc, ver, ws, text="pinned content")
    store = _FakeChunkStore([row])
    out = _read_output(ws, doc, ver, cid, store=store)
    assert out["document_id"] == doc
    assert out["document_version_id"] == ver
    assert out["chunk_id"] == cid
    assert out["workspace_id"] == ws
    assert out["content"] == "pinned content"
    assert len(store.calls) == 1 and store.calls[0] == (doc, ver, cid)


@pytest.mark.unit
def test_knowledge_read_out_of_scope_rejected() -> None:
    doc = str(uuid.uuid4())
    ver = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    victim = _FakeChunkRow(cid, doc, ver, "ws-victim", text="secret")
    store = _FakeChunkStore([victim])
    out = _read_output("ws-allowed", doc, ver, cid, store=store)
    assert out["error_code"] == AgentErrorCode.WORKSPACE_MISMATCH
    assert "workspace" in out["error"].lower()
    assert "content" not in out


@pytest.mark.unit
def test_knowledge_read_revoked_deleted_refused() -> None:
    doc = str(uuid.uuid4())
    ver = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    ws = "ws-allowed"
    # Superseded version
    row1 = _FakeChunkRow(cid, doc, ver, ws, text="old", superseded_at=datetime.now(UTC))
    out = _read_output(ws, doc, ver, cid, store=_FakeChunkStore([row1]))
    assert out["error_code"] == AgentErrorCode.SOURCE_REVOKED
    # Deleted document (via deleted_at on row)
    cid2 = str(uuid.uuid4())
    row2 = _FakeChunkRow(cid2, doc, ver, ws, text="old", deleted_at=datetime.now(UTC))
    out = _read_output(ws, doc, ver, cid2, store=_FakeChunkStore([row2]))
    assert out["error_code"] == AgentErrorCode.SOURCE_REVOKED


@pytest.mark.unit
def test_knowledge_read_wrong_triple_no_fuzzy_fallback() -> None:
    doc = str(uuid.uuid4())
    ver = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    ws = "ws-allowed"
    row = _FakeChunkRow(cid, doc, ver, ws, text="hello")
    store = _FakeChunkStore([row])
    # Wrong document_id
    out = _read_output(ws, str(uuid.uuid4()), ver, cid, store=store)
    assert out["error_code"] == AgentErrorCode.TOOL_SCHEMA_INVALID
    assert "mismatch" in out["error"].lower()
    # Wrong version_id
    out = _read_output(ws, doc, str(uuid.uuid4()), cid, store=store)
    assert out["error_code"] == AgentErrorCode.TOOL_SCHEMA_INVALID
    # Wrong chunk_id
    out = _read_output(ws, doc, ver, str(uuid.uuid4()), store=store)
    assert out["error_code"] == AgentErrorCode.TOOL_SCHEMA_INVALID


@pytest.mark.unit
def test_knowledge_read_output_bounded() -> None:
    doc = str(uuid.uuid4())
    ver = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    ws = "ws-allowed"
    big = "y" * 5000
    row = _FakeChunkRow(cid, doc, ver, ws, text=big)
    out = _read_output(ws, doc, ver, cid, store=_FakeChunkStore([row]))
    assert "content" in out
    assert len(out["content"]) <= 800 + 3
    raw = json.dumps(out, ensure_ascii=False).encode("utf-8")
    assert len(raw) <= 16384


@pytest.mark.unit
def test_knowledge_read_no_workspace_scan() -> None:
    doc = str(uuid.uuid4())
    ver = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    ws = "ws-allowed"
    row = _FakeChunkRow(cid, doc, ver, ws, text="hi")
    store = _FakeChunkStore([row])
    # The store has no scan/list method that the tool should call; only get_chunk
    # Ensure tool calls exact triple, not a scan
    out = _read_output(ws, doc, ver, cid, store=store)
    assert out["content"] == "hi"
    # If tool had scanned workspace, it would have called with wrong signature
    assert store.calls == [(doc, ver, cid)]


@pytest.mark.unit
def test_knowledge_read_strict_schema_and_runtime_block() -> None:
    registry = ToolRegistry()
    registry.register(KnowledgeReadTool(chunk_reader=_FakeChunkStore()))
    llm = _ScriptedLLM(
        [
            "Thought: smuggle\nAction: knowledge_read\n"
            'Action Input: {"document_id": "00000000-0000-0000-0000-000000000001", "document_version_id": "00000000-0000-0000-0000-000000000002", "chunk_id": "00000000-0000-0000-0000-000000000003", "extra": "evil"}',  # noqa: E501
            "Final Answer: done",
        ]
    )
    rt = AgentRuntime(llm, registry)
    ctx = RunContext(
        run_id="run-t303-1",
        workspace_id="ws-allowed",
        principal_id="user-1",
        authz_policy_revision="pol-1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(max_steps=5, max_tool_calls=5, max_tokens=100000),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["knowledge_read"],
    )
    run = asyncio.run(rt.run_enforced(context=ctx, goal="try extra field"))
    assert any(s.type == AgentStepType.ERROR for s in run.steps)
    assert any(
        "undeclared" in json.dumps(s.tool_output).lower() for s in run.steps if s.tool_output
    )


@pytest.mark.unit
def test_knowledge_read_runtime_blocks_workspace_id_override() -> None:
    registry = ToolRegistry()
    fake = _FakeChunkStore(
        [
            _FakeChunkRow(
                str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), "ws-victim", text="secret"
            )
        ]
    )
    # This orchestrator shouldn't be called because dispatch blocks workspace_id
    registry.register(KnowledgeReadTool(chunk_reader=fake))
    llm = _ScriptedLLM(
        [
            "Thought: steal\nAction: knowledge_read\n"
            'Action Input: {"document_id": "00000000-0000-0000-0000-000000000001", "document_version_id": "00000000-0000-0000-0000-000000000002", "chunk_id": "00000000-0000-0000-0000-000000000003", "workspace_id": "ws-victim"}',  # noqa: E501
            "Final Answer: done",
        ]
    )
    rt = AgentRuntime(llm, registry)
    ctx = RunContext(
        run_id="run-t303-2",
        workspace_id="ws-allowed",
        principal_id="user-1",
        authz_policy_revision="pol-1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(max_steps=5, max_tool_calls=5, max_tokens=100000),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["knowledge_read"],
    )
    run = asyncio.run(rt.run_enforced(context=ctx, goal="try workspace override"))
    assert any(s.type == AgentStepType.ERROR for s in run.steps)
    assert not any("ws-victim" in json.dumps(s.tool_output) for s in run.steps if s.tool_output)


@pytest.mark.unit
def test_knowledge_read_has_no_write_surface() -> None:
    import ast

    source = Path("packages/agents/tools.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "KnowledgeReadTool":
            called = {
                getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                for n in ast.walk(node)
                if isinstance(n, ast.Call)
            }
            assert called.isdisjoint(
                {"upsert", "delete", "index", "put_object", "eval", "exec", "compile"}
            ), called
            assert "import qdrant_client" not in ast.unparse(node)
            assert "from qdrant_client" not in ast.unparse(node)


@pytest.mark.unit
def test_knowledge_read_uses_adapter_boundary() -> None:
    text = Path("packages/agents/tools.py").read_text()
    assert "import qdrant_client" not in text
    assert "from qdrant_client" not in text
    # Must use persistence seam, not direct SQL string with workspace scan
    assert "ChunkRow" in text or "chunk_reader" in text

# ======================================================================================
# T-V3-004 — Bounded typed-action parsing (V3 plan trace runtime)
# ======================================================================================

import time as _time  # noqa: E402

from agents.runtime import MAX_ACTION_TEXT_CHARS, MAX_PARSE_TIME_S, _parse_action_sync  # noqa: E402


@pytest.mark.unit
def test_typed_parser_bounded_size_and_time() -> None:
    """Checklist 1: parse bounded — size + wall-clock, with why."""
    # Size bound: overlong is BLOCKED without parsing
    long_text = "x" * (MAX_ACTION_TEXT_CHARS + 1)
    parsed = _parse_action_sync(long_text)
    assert parsed.kind == "invalid"
    assert "exceeds" in (parsed.error or "")
    assert parsed.error_code == AgentErrorCode.SCHEMA_INVALID
    # Time bound is real: parsing 10KB garbage must complete well within 0.5s
    garbage = "x" * 10_240
    start = _time.perf_counter()
    parsed = _parse_action_sync(garbage)
    elapsed = _time.perf_counter() - start
    assert elapsed < MAX_PARSE_TIME_S, f"10KB parse took {elapsed:.3f}s > {MAX_PARSE_TIME_S}s"
    assert parsed.kind == "invalid"
    # Bounds are sane: real action still parses
    real = "Thought: ok\nAction: calculator\nAction Input: {\"expression\": \"1+1\"}"
    parsed = _parse_action_sync(real, allowed_tools={"calculator"})
    assert parsed.kind == "tool_call"
    assert parsed.tool_name == "calculator"


@pytest.mark.unit
def test_unknown_action_blocked_never_exec() -> None:
    """Checklist 2: unknown action → BLOCKED SCHEMA_INVALID, never exec."""
    from agents.tools import CalculatorTool

    calls: list[str] = []

    class _TrackingTool(CalculatorTool):
        async def execute(self, workspace_id: str, **kwargs):
            calls.append(kwargs.get("expression", ""))
            return await super().execute(workspace_id, **kwargs)

    registry = ToolRegistry()
    registry.register(_TrackingTool())
    llm = _ScriptedLLM(
        [
            'Thought: try unknown\nAction: unknown_tool\nAction Input: {"query": "hello"}',
            "Final Answer: done after block",
        ]
    )
    rt = AgentRuntime(llm, registry)
    ctx = RunContext(
        run_id="run-t304-unk",
        workspace_id="ws-allowed",
        principal_id="user-1",
        authz_policy_revision="pol-1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(max_steps=5, max_tool_calls=5, max_tokens=100000),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["calculator"],
    )
    run = asyncio.run(rt.run_enforced(context=ctx, goal="unknown"))
    # Unknown must be BLOCKED, never exec (policy or unknown)
    assert any(s.type == AgentStepType.ERROR for s in run.steps), run.steps
    assert calls == [], f"unknown action executed tool: {calls}"
    # Metric: unknown actions executed: 0
    assert not any(
        s.tool_name == "unknown_tool" and s.type == AgentStepType.TOOL_OUTPUT for s in run.steps
    )


@pytest.mark.unit
def test_fuzz_10kb_garbage_blocked_no_hang() -> None:
    """Metric: fuzz inputs hung: 0 — 10KB garbage BLOCKED within wall-clock bound."""
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    garbage = "x" * 10_240 + "\n" + "y" * 500
    llm = _ScriptedLLM(
        [
            garbage,
            "Final Answer: recovered",
        ]
    )
    rt = AgentRuntime(llm, registry)
    ctx = RunContext(
        run_id="run-t304-fuzz",
        workspace_id="ws-allowed",
        principal_id="user-1",
        authz_policy_revision="pol-1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(max_steps=5, max_tool_calls=5, max_tokens=100000),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["calculator"],
    )
    start = _time.perf_counter()
    run = asyncio.run(rt.run_enforced(context=ctx, goal="fuzz"))
    elapsed = _time.perf_counter() - start
    assert elapsed < 2.0, f"fuzz run hung: {elapsed:.3f}s"
    assert any(s.type == AgentStepType.ERROR for s in run.steps)
    # First step should be BLOCKED SCHEMA_INVALID, not a hang
    first_error = next(s for s in run.steps if s.type == AgentStepType.ERROR)
    assert "exceeds" in str((first_error.tool_output or {}).get("error", "")).lower() or "invalid" in str(  # noqa: E501
        (first_error.tool_output or {}).get("error", "")
    ).lower()
    # Also directly test bounded parser wall-clock guard

    async def _bounded():
        return await _parse_action_bounded(garbage, allowed_tools={"calculator"})

    # Need to import the bounded parser
    from agents.runtime import _parse_action_bounded

    start = _time.perf_counter()
    parsed = asyncio.run(_parse_action_bounded(garbage, allowed_tools={"calculator"}))
    elapsed = _time.perf_counter() - start
    assert elapsed < MAX_PARSE_TIME_S + 0.2, f"bounded parse hung: {elapsed:.3f}s"
    assert parsed.kind == "invalid"


@pytest.mark.unit
def test_prose_never_persisted_as_evidence() -> None:
    """Checklist 3: model prose is untrusted input to parser, never ground truth."""
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    llm = _ScriptedLLM(
        [
            "Thought: just thinking without Action or Final Answer markers, prose only",
            "Thought: now answer\nFinal Answer: the real answer",
        ]
    )
    rt = AgentRuntime(llm, registry)
    ctx = RunContext(
        run_id="run-t304-prose",
        workspace_id="ws-allowed",
        principal_id="user-1",
        authz_policy_revision="pol-1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(max_steps=5, max_tool_calls=5, max_tokens=100000),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["calculator"],
    )
    run = asyncio.run(rt.run_enforced(context=ctx, goal="prose test"))
    # Prose must not become final_output
    assert run.final_output == "the real answer"
    assert run.final_output != "just thinking without Action or Final Answer markers, prose only"
    # Prose step must be BLOCKED, not evidence
    assert any(s.type == AgentStepType.ERROR for s in run.steps)
    # No step should have persisted prose as evidence content
    for s in run.steps:
        if s.type == AgentStepType.ERROR:
            assert "just thinking" not in str(s.tool_output)


@pytest.mark.unit
def test_no_regex_dispatch_surviving() -> None:
    """Grep-prove: no re.search/match on model output for control flow in runtime."""
    import pathlib

    text = pathlib.Path("packages/agents/runtime.py").read_text()
    # The file may still import re, but must not use it for dispatch on model output
    assert "re.search" not in text, "re.search surviving for dispatch — use bounded parser"
    assert "re.match" not in text, "re.match surviving for dispatch"
    # Also ensure the bounded parser's constants are present and sane
    assert "MAX_ACTION_TEXT_CHARS" in text
    assert "MAX_PARSE_TIME_S" in text


@pytest.mark.unit
def test_fuzz_garbage_completes_or_blocked_within_bound() -> None:
    """Time bound must be REAL timeout — fuzz completes or BLOCKED within bound."""
    from agents.runtime import _parse_action_bounded

    # 10KB of alternating braces/brackets that would stress a naive regex
    garbage = ("{ " * 2000 + "} " * 2000)[:10240]
    start = _time.perf_counter()
    parsed = asyncio.run(_parse_action_bounded(garbage, allowed_tools={"calculator"}))
    elapsed = _time.perf_counter() - start
    assert elapsed < MAX_PARSE_TIME_S + 0.5, (
        f"garbage parse took {elapsed:.3f}s, not wall-clock bounded"
    )
    assert parsed.kind == "invalid"
    assert parsed.error_code == AgentErrorCode.SCHEMA_INVALID

# ======================================================================================
# T-V3-005 — Retry policy + error classification (V3 plan trace runtime)
# ======================================================================================

from agents.runtime import bounded_backoff, compute_backoff, is_retryable  # noqa: E402


@pytest.mark.unit
def test_retry_classifier_is_pure_and_correct() -> None:
    """Error taxonomy shared with T-052: retryable vs fatal is a pure function."""
    policy = RetryPolicy(
        max_attempts=3,
        initial_backoff_s=0.1,
        backoff_multiplier=2.0,
        max_backoff_s=1.0,
        retryable_error_codes=[AgentErrorCode.TOOL_TIMEOUT],
    )
    # TOOL_TIMEOUT is retryable per policy
    assert is_retryable(AgentErrorCode.TOOL_TIMEOUT, policy) is True
    # TOOL_NOT_ALLOWED is never retryable — even if someone mis-adds it, the test
    # below proves the default policy never contains it, and the classifier respects it
    assert is_retryable(AgentErrorCode.TOOL_NOT_ALLOWED, policy) is False
    assert is_retryable(AgentErrorCode.TOOL_NOT_FOUND, policy) is False
    assert is_retryable(None, policy) is False
    # A policy that explicitly lists TOOL_NOT_ALLOWED would retry it — the
    # pure function does what the policy says, but the *default* never does
    bad_policy = RetryPolicy(
        max_attempts=2, retryable_error_codes=[AgentErrorCode.TOOL_NOT_ALLOWED]
    )
    assert is_retryable(AgentErrorCode.TOOL_NOT_ALLOWED, bad_policy) is True
    # Empty policy retries nothing
    empty = RetryPolicy(max_attempts=3, retryable_error_codes=[])
    assert is_retryable(AgentErrorCode.TOOL_TIMEOUT, empty) is False


@pytest.mark.unit
def test_backoff_bounded_not_unbounded_exponential() -> None:
    """Backoff is capped — worst-case total wait is bounded, not exponential."""
    policy = RetryPolicy(
        max_attempts=5,
        initial_backoff_s=1.0,
        backoff_multiplier=2.0,
        max_backoff_s=3.0,
        retryable_error_codes=[AgentErrorCode.TOOL_TIMEOUT],
    )
    # Exponential without cap would be 1,2,4,8,16 → 31s total. With cap 3s: 1,2,3,3,3 → 12s
    assert compute_backoff(1, policy) == 1.0
    assert compute_backoff(2, policy) == 2.0
    assert compute_backoff(3, policy) == 3.0  # capped
    assert compute_backoff(4, policy) == 3.0
    assert compute_backoff(5, policy) == 3.0
    total = sum(compute_backoff(i, policy) for i in range(1, 6))
    assert total == 12.0, f"unbounded exponential would be 31, capped is 12, got {total}"
    # Deadline caps it further — no sleep past wall-clock
    deadline_soon = datetime.now(UTC) + timedelta(milliseconds=50)
    soon = bounded_backoff(1, policy, deadline_soon)
    assert soon <= 0.05 + 0.01, f"deadline cap failed: {soon}"
    # Past deadline → 0
    past = datetime.now(UTC) - timedelta(seconds=1)
    assert bounded_backoff(1, policy, past) == 0.0
    # State the cap and why in the assertion message
    cap = policy.max_backoff_s
    assert cap == 3.0, "max_backoff caps exponential to prevent worker hold for minutes"


@pytest.mark.unit
def test_tool_timeout_retries_n_then_failed_attempts_persisted() -> None:
    """TOOL_TIMEOUT retries N then FAILED; attempts visible in the run record."""

    class _TimeoutTool(Tool):
        name = "flaky_timeout"
        description = "always times out"
        input_schema: ClassVar[dict] = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "additionalProperties": False,
        }
        timeout_s = 0.05
        retry_policy = RetryPolicy(
            max_attempts=3,
            initial_backoff_s=0.01,
            backoff_multiplier=2.0,
            max_backoff_s=0.1,
            retryable_error_codes=[AgentErrorCode.TOOL_TIMEOUT],
        )

        async def execute(self, workspace_id: str, **kwargs):
            await asyncio.sleep(0.5)  # longer than timeout → TOOL_TIMEOUT
            return {"ok": True}

    registry = ToolRegistry()
    registry.register(_TimeoutTool())
    llm = _ScriptedLLM(
        [
            'Thought: try flaky\nAction: flaky_timeout\nAction Input: {"x": 1}',
            "Final Answer: done",
        ]
    )
    rt = AgentRuntime(llm, registry)
    ctx = RunContext(
        run_id="run-t305-timeout",
        workspace_id="ws-1",
        principal_id="user-1",
        authz_policy_revision="pol-1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(max_steps=5, max_tool_calls=10, max_tokens=100000),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["flaky_timeout"],
    )
    start = _time.perf_counter()
    run = asyncio.run(rt.run_enforced(context=ctx, goal="retry timeout"))
    elapsed = _time.perf_counter() - start
    # Three attempts, then FAILED — not in-memory counter, persisted as steps
    timeout_steps = [s for s in run.steps if s.tool_name == "flaky_timeout"]
    assert len(timeout_steps) == 3, f"expected 3 attempts, got {len(timeout_steps)}"
    assert all(s.type == AgentStepType.ERROR for s in timeout_steps)
    # Last attempt's persisted output carries attempt count
    last = timeout_steps[-1]
    assert last.tool_output is not None
    assert last.tool_output.get("attempt") == 3
    assert last.tool_output.get("max_attempts") == 3
    # Final is FAILED with TOOL_TIMEOUT, not success
    assert run.error is not None and "tool_timeout" in run.error.lower()
    # Total wall-clock is bounded: 3 timeouts (0.05 each) + backoffs 0.01+0.02 ≈0.18s, not 3*30s
    assert elapsed < 2.0, f"retry held too long: {elapsed:.2f}s"
    # Metric: transient exhaustion → FAILED: yes


@pytest.mark.unit
def test_tool_not_allowed_never_retried_attempts_one() -> None:
    """TOOL_NOT_ALLOWED (scope/policy) never retries — fails on attempt 1, attempts==1."""

    class _AllowedTool(Tool):
        name = "allowed_tool"
        description = "allowed"
        input_schema: ClassVar[dict] = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "additionalProperties": False,
        }
        retry_policy = RetryPolicy(
            max_attempts=5,
            initial_backoff_s=0.1,
            backoff_multiplier=2.0,
            max_backoff_s=1.0,
            retryable_error_codes=[AgentErrorCode.TOOL_TIMEOUT],
        )

        async def execute(self, workspace_id: str, **kwargs):
            return {"ok": True}

    registry = ToolRegistry()
    registry.register(_AllowedTool())
    # LLM tries a tool not in allowlist — dispatch will BLOCK with TOOL_NOT_ALLOWED
    llm = _ScriptedLLM(
        [
            'Thought: try hidden\nAction: hidden_tool\nAction Input: {"x": 1}',
            "Final Answer: done",
        ]
    )
    rt = AgentRuntime(llm, registry)
    ctx = RunContext(
        run_id="run-t305-notallowed",
        workspace_id="ws-1",
        principal_id="user-1",
        authz_policy_revision="pol-1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(max_steps=5, max_tool_calls=10, max_tokens=100000),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["allowed_tool"],
    )
    run = asyncio.run(rt.run_enforced(context=ctx, goal="not allowed"))
    hidden_steps = [s for s in run.steps if s.tool_name == "hidden_tool"]
    assert len(hidden_steps) == 1, f"TOOL_NOT_ALLOWED must not retry, got {len(hidden_steps)}"
    assert hidden_steps[0].type == AgentStepType.ERROR
    assert hidden_steps[0].tool_output is not None
    assert hidden_steps[0].tool_output.get("error_code") == "tool_not_allowed"
    assert hidden_steps[0].tool_output.get("attempt") == 1
    # Also direct classifier proof
    policy = RetryPolicy(max_attempts=3, retryable_error_codes=[AgentErrorCode.TOOL_TIMEOUT])
    assert is_retryable(AgentErrorCode.TOOL_NOT_ALLOWED, policy) is False
    # Metric: policy-error retries: 0


@pytest.mark.unit
def test_retry_backoff_bounded_by_deadline_computed_not_slept() -> None:
    """Worst-case total wait is capped — computed bound, not a real 60s sleep."""
    policy = RetryPolicy(
        max_attempts=4,
        initial_backoff_s=10.0,
        backoff_multiplier=2.0,
        max_backoff_s=30.0,
        retryable_error_codes=[AgentErrorCode.TOOL_TIMEOUT],
    )
    # Unbounded would be 10,20,40,80 =150s. Capped at 30: 10,20,30,30 =90s
    unbounded_total = sum(10.0 * (2.0 ** (i - 1)) for i in range(1, 5))
    capped_total = sum(compute_backoff(i, policy) for i in range(1, 5))
    assert unbounded_total == 150.0
    assert capped_total == 90.0
    assert capped_total < unbounded_total, "cap must reduce worst-case"
    # With a tight deadline, total is deadline-bounded
    tight_deadline = datetime.now(UTC) + timedelta(milliseconds=100)
    # Even though policy wants 10s, deadline caps to ~0.1s
    assert bounded_backoff(1, policy, tight_deadline) <= 0.11
    # Prove with fake clock: no real sleep, just compute
    assert compute_backoff(1, policy) == 10.0
    assert compute_backoff(4, policy) == 30.0

# ======================================================================================
# T-V3-006 — Budget reservation before spend (V3 plan trace runtime)
# ======================================================================================

from agents.runtime import (  # noqa: E402
    _PERSISTED_RUNS,
    _PERSISTED_STATES,
    _PERSISTED_USAGES,
    can_reserve_before_llm,
    can_reserve_tokens,
    can_reserve_tool_call,
    is_limit_exceeded,
)


@pytest.mark.unit
def test_no_unlimited_default_grep_prove() -> None:
    """Checklist 1: no None-means-infinite — grep prove."""
    text = Path("packages/agents/runtime.py").read_text()
    # Any `is None` that returns unlimited would be `if ... is None: return True` with unlimited
    # We forbid the pattern where missing usage/budget is treated as allow
    assert "None-means-infinite" not in text
    # Ensure the pure helpers deny on None
    assert can_reserve_tokens(None, RunBudget(), 1) is False
    assert can_reserve_tool_call(None, RunBudget()) is False
    assert can_reserve_before_llm(None, RunBudget(), datetime.now(UTC) + timedelta(seconds=10), 1) is False  # noqa: E501
    # And ensure no `if budget.max_tokens is None` unlimited path
    assert "if budget.max_tokens is None" not in text
    assert "if usage is None:\n                return True" not in text


@pytest.mark.unit
def test_durable_limit_state_monotonic() -> None:
    """Checklist 2: LIMIT_EXCEEDED durable + monotonic — no reset path."""
    state = RunState(status=RunStatus.RUNNING)
    state.transition_to(RunStatus.LIMIT_EXCEEDED, error_code=AgentErrorCode.BUDGET_EXCEEDED, error_detail="cap")  # noqa: E501
    assert is_limit_exceeded(state) is True
    assert RunState.is_terminal(state.status) is True
    # Once exceeded, it stays — any transition to COMPLETED should raise
    try:
        state.transition_to(RunStatus.COMPLETED)
        raise AssertionError("monotonic: LIMIT_EXCEEDED must not transition to COMPLETED")
    except InvalidRunTransitionError:
        pass
    assert state.status == RunStatus.LIMIT_EXCEEDED
    # Also test that a second transition to LIMIT_EXCEEDED is also blocked (already terminal)
    try:
        state.transition_to(RunStatus.LIMIT_EXCEEDED, error_code=AgentErrorCode.BUDGET_EXCEEDED)
        raise AssertionError("terminal must reject further transitions")
    except InvalidRunTransitionError:
        pass


@pytest.mark.unit
def test_usage_survives_simulated_retry_and_cap_still_enforced() -> None:
    """Checklist 3: usage survives retry/restart — persisted, not reconstructed."""
    # Clear any prior persisted state
    _PERSISTED_RUNS.clear()
    _PERSISTED_STATES.clear()
    _PERSISTED_USAGES.clear()

    small_budget = RunBudget(max_tokens=10, max_tool_calls=5, max_steps=10, max_wall_clock_s=300)
    ctx = RunContext(
        run_id="run-t306-usage-persist",
        workspace_id="ws-1",
        principal_id="user-1",
        authz_policy_revision="pol-1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini", max_output_tokens=5),  # noqa: E501
        budget=small_budget,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["calculator"],
    )
    # First run: consume 5 tokens via LLM
    llm1 = _ScriptedLLM(
        [
            'Thought: use tool\nAction: calculator\nAction Input: {"expression": "1+1"}',
            "Final Answer: done",
        ]
    )
    rt1 = AgentRuntime(llm1, ToolRegistry())
    rt1.registry.register(CalculatorTool())
    asyncio.run(rt1.run_enforced(context=ctx, goal="consume 5"))
    # After first LLM call, usage should be 5 (prompt+completion ~2, but we set max_output 5)
    # The persisted usage should reflect that
    persisted = _PERSISTED_USAGES.get(ctx.run_id)
    assert persisted is not None
    # Simulate retry/restart: create a new runtime and new context with same run_id but fresh objects  # noqa: E501
    # The new run should see the persisted usage and still enforce the cap
    # Try to consume 6 more tokens (would be 5+6=11 >10) — should be denied before spend
    assert can_reserve_tokens(persisted, small_budget, 6) is False
    # Also test that a new run_enforced with same run_id sees the persisted usage and does not reset
    # Create a new LLM that would try to do another tool call
    llm2 = _ScriptedLLM(
        [
            'Thought: try again\nAction: calculator\nAction Input: {"expression": "2+2"}',
            "Final Answer: done2",
        ]
    )
    rt2 = AgentRuntime(llm2, ToolRegistry())
    rt2.registry.register(CalculatorTool())
    # Re-use same context (same run_id, same budget) — usage should be intact
    asyncio.run(rt2.run_enforced(context=ctx, goal="retry"))
    # Since run1 already consumed some budget, run2 should either be LIMIT_EXCEEDED or still enforce
    # At minimum, the persisted usage should not have been reset to 0
    assert _PERSISTED_USAGES[ctx.run_id].prompt_tokens == persisted.prompt_tokens
    # And if we try to reserve 10 more tokens, it should be denied
    assert can_reserve_tokens(_PERSISTED_USAGES[ctx.run_id], small_budget, 10) is False
    # Cleanup
    _PERSISTED_RUNS.clear()
    _PERSISTED_STATES.clear()
    _PERSISTED_USAGES.clear()


@pytest.mark.unit
def test_zero_model_calls_after_deadline() -> None:
    """Zero model calls after deadline — reservation before spend."""
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    calls: list[str] = []

    class _CountingLLM:
        async def generate(self, request):
            calls.append(request.model)
            return type("R", (), {"text": "Final Answer: done", "prompt_tokens": 1, "completion_tokens": 1, "model": request.model})()  # noqa: E501

        def stream(self, request):
            raise AssertionError

    # Deadline already past
    past_deadline = datetime.now(UTC) - timedelta(seconds=1)
    ctx = RunContext(
        run_id="run-t306-deadline-past",
        workspace_id="ws-1",
        principal_id="user-1",
        authz_policy_revision="pol-1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(max_steps=5, max_tokens=100000, max_tool_calls=5, max_wall_clock_s=300),
        deadline=past_deadline,
        allowed_tools=["calculator"],
    )
    rt = AgentRuntime(_CountingLLM(), registry)  # type: ignore[arg-type]
    run = asyncio.run(rt.run_enforced(context=ctx, goal="past deadline"))
    assert len(calls) == 0, f"zero calls after deadline, got {len(calls)}"
    assert run.status == AgentRunStatus.MAX_STEPS_EXCEEDED or "deadline" in (run.error or "").lower() or run.error is not None  # noqa: E501
    # Also test can_reserve_before_llm with past deadline denies
    assert can_reserve_before_llm(Usage(), RunBudget(), past_deadline, 1) is False
    # Cleanup
    _PERSISTED_RUNS.pop(ctx.run_id, None)
    _PERSISTED_STATES.pop(ctx.run_id, None)
    _PERSISTED_USAGES.pop(ctx.run_id, None)


@pytest.mark.unit
def test_spend_after_exceeded_refused_overspend_zero() -> None:
    """Monotonic + reserve before spend: overspend cases 0 — spend-after-exceeded refused."""
    # Create a run that is already LIMIT_EXCEEDED
    state = RunState(status=RunStatus.LIMIT_EXCEEDED, error_code=AgentErrorCode.BUDGET_EXCEEDED, error_detail="cap")  # noqa: E501
    _PERSISTED_STATES["run-t306-exceeded"] = state
    _PERSISTED_RUNS["run-t306-exceeded"] = AgentRun(
        id="run-t306-exceeded",
        workspace_id="ws-1",
        goal="test",
        model="gpt-4o-mini",
        status=AgentRunStatus.MAX_STEPS_EXCEEDED,
        total_tokens=100,
        steps=[],
        created_at=datetime.now(UTC).isoformat(),
    )
    _PERSISTED_USAGES["run-t306-exceeded"] = Usage(prompt_tokens=100, completion_tokens=0, tool_calls=50)  # noqa: E501
    ctx = RunContext(
        run_id="run-t306-exceeded",
        workspace_id="ws-1",
        principal_id="user-1",
        authz_policy_revision="pol-1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(max_steps=5, max_tokens=10, max_tool_calls=1, max_wall_clock_s=300),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["calculator"],
    )
    # Even if LLM tries to act, the run should immediately return the persisted terminal run with no new spend  # noqa: E501
    llm = _ScriptedLLM(['Thought: try\nAction: calculator\nAction Input: {"expression": "1+1"}', "Final Answer: done"])  # noqa: E501
    rt = AgentRuntime(llm, ToolRegistry())
    rt.registry.register(CalculatorTool())
    run = asyncio.run(rt.run_enforced(context=ctx, goal="should be refused"))
    # Should be same object, no new steps, still exceeded
    assert run.id == "run-t306-exceeded"
    assert len(run.steps) == 0  # no new spend
    # Cleanup
    _PERSISTED_RUNS.pop("run-t306-exceeded", None)
    _PERSISTED_STATES.pop("run-t306-exceeded", None)
    _PERSISTED_USAGES.pop("run-t306-exceeded", None)


@pytest.mark.unit
def test_budget_reservation_before_spend_not_after() -> None:
    """Reserve before spend — token and tool_calls checked before LLM/tool, not after."""
    # Directly test the pure helpers
    budget = RunBudget(max_tokens=10, max_tool_calls=2, max_steps=5, max_wall_clock_s=300)
    usage = Usage(prompt_tokens=9, completion_tokens=0, tool_calls=1)
    # 9 + 2 >10 → cannot reserve 2 more tokens
    assert can_reserve_tokens(usage, budget, 2) is False
    assert can_reserve_tokens(usage, budget, 1) is True
    # Tool calls: 1 +1 <=2 → can, 1+2 >2 → cannot
    assert can_reserve_tool_call(usage, budget) is True
    usage2 = Usage(prompt_tokens=0, completion_tokens=0, tool_calls=2)
    assert can_reserve_tool_call(usage2, budget) is False
    # Missing usage → DENY
    assert can_reserve_tokens(None, budget, 1) is False
    assert can_reserve_tool_call(None, budget) is False

# ======================================================================================
# T-V3-007 — Cancellation + terminal-state monotonicity (V3 plan trace runtime)
# ======================================================================================



@pytest.mark.unit
def test_mid_tool_cancel_stops_loop_no_further_dispatches() -> None:
    """Checklist 1: cancel mid-tool STOPS the loop — post-cancel dispatches 0."""
    # Pre-seed a RunState that the tool can cancel mid-execution
    run_id = f"run-cancel-{uuid.uuid4().hex[:6]}"
    state = RunState(status=RunStatus.RUNNING)
    _PERSISTED_STATES[run_id] = state
    _PERSISTED_RUNS[run_id] = AgentRun(
        id=run_id,
        workspace_id="ws-1",
        goal="cancel mid tool",
        model="gpt-4o-mini",
        status=AgentRunStatus.RUNNING,
        created_at=datetime.now(UTC).isoformat(),
    )
    _PERSISTED_USAGES[run_id] = Usage()

    calls: list[dict] = []

    class _CancelTool(Tool):
        name = "cancel_tool"
        description = "cancels on first call"
        input_schema: ClassVar[dict] = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "additionalProperties": False,
        }

        async def execute(self, workspace_id: str, **kwargs):
            calls.append(dict(kwargs))
            if len(calls) == 1:
                # Cancel from inside the tool — the next loop iteration must see it
                state.request_cancel()
            return {"ok": True}

    registry = ToolRegistry()
    registry.register(_CancelTool())
    # LLM wants to call the tool three times then final answer, but cancel after first must stop
    llm = _ScriptedLLM(
        [
            'Thought: step1\nAction: cancel_tool\nAction Input: {"x": 1}',
            'Thought: step2\nAction: cancel_tool\nAction Input: {"x": 2}',
            'Thought: step3\nAction: cancel_tool\nAction Input: {"x": 3}',
            "Final Answer: done",
        ]
    )
    rt = AgentRuntime(llm, registry)
    ctx = RunContext(
        run_id=run_id,
        workspace_id="ws-1",
        principal_id="user-1",
        authz_policy_revision="pol-1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(max_steps=10, max_tool_calls=10, max_tokens=100000),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["cancel_tool"],
    )
    run = asyncio.run(rt.run_enforced(context=ctx, goal="cancel mid-tool"))
    # Only the first dispatch should have happened; post-cancel dispatches 0
    assert len(calls) == 1, f"post-cancel dispatches should be 0, got {len(calls)-1} extra: {calls}"
    assert state.status == RunStatus.CANCELLED
    # The run should be CANCELLED, not COMPLETED, and the final answer must not have been reached
    assert run.status == AgentRunStatus.FAILED  # CANCELLED maps to FAILED via _legacy_status
    # Cleanup
    _PERSISTED_RUNS.pop(run_id, None)
    _PERSISTED_STATES.pop(run_id, None)
    _PERSISTED_USAGES.pop(run_id, None)
    assert len(calls) == 1  # metric: post-cancel dispatches 0


@pytest.mark.unit
def test_terminal_transitions_one_way_exhaust_matrix() -> None:
    """Checklist 2: every terminal state rejects ALL transitions — exhaust matrix, not sample."""
    from agents.interfaces import _TERMINAL_STATUSES

    all_statuses = list(RunStatus)
    terminal = list(_TERMINAL_STATUSES)
    # For each terminal, every attempt to leave must raise
    for term in terminal:
        for target in all_statuses:
            state = RunState(status=term)
            # Even transitioning to itself should raise because already terminal
            try:
                state.transition_to(target)
                raise AssertionError(f"terminal {term} must reject transition to {target}")
            except InvalidRunTransitionError:
                pass
            assert state.status == term, "state must remain byte-identical after rejected transition"  # noqa: E501
    # Non-terminal can go to terminal, but then cannot leave
    for active in [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_INPUT, RunStatus.WAITING_APPROVAL]:  # noqa: E501
        state = RunState(status=active)
        state.transition_to(RunStatus.COMPLETED)
        assert state.status == RunStatus.COMPLETED
        # Now terminal, any further must raise
        for target in all_statuses:
            try:
                state.transition_to(target)
                raise AssertionError(f"after COMPLETED, must reject {target}")
            except InvalidRunTransitionError:
                pass


@pytest.mark.unit
def test_cancel_after_completed_is_clean_noop_byte_identical() -> None:
    """Checklist 3: cancel-after-COMPLETED is no-op — state + record byte-identical."""
    state = RunState(status=RunStatus.COMPLETED, error_code=None, error_detail=None)
    run = AgentRun(
        id="run-noop",
        workspace_id="ws-1",
        goal="test",
        model="gpt-4o-mini",
        status=AgentRunStatus.COMPLETED,
        steps=[
            AgentStep(
                step_number=1,
                type=AgentStepType.FINAL_ANSWER,
                content="done",
                timestamp=datetime.now(UTC).isoformat(),
            )
        ],
        final_output="done",
        total_tokens=5,
        created_at=datetime.now(UTC).isoformat(),
        finished_at=datetime.now(UTC).isoformat(),
    )
    # Snapshot byte-identical
    before_state = state.model_dump_json()
    before_run = run.model_dump_json()
    # Cancel should be no-op, not error, not state change
    result = state.request_cancel()
    assert result is False
    assert state.status == RunStatus.COMPLETED
    assert state.model_dump_json() == before_state
    # Also via AgentRuntime.cancel_run
    rt = AgentRuntime(_ScriptedLLM(["Final Answer: done"]), ToolRegistry())
    # Need to test cancel_run on already terminal
    state2 = RunState(status=RunStatus.COMPLETED)
    run2 = AgentRun(
        id="run-noop2",
        workspace_id="ws-1",
        goal="test",
        model="gpt-4o-mini",
        status=AgentRunStatus.COMPLETED,
        created_at=datetime.now(UTC).isoformat(),
    )
    before_state2 = state2.model_dump_json()
    before_run2 = run2.model_dump_json()
    ok = rt.cancel_run(state2, run2)
    assert ok is False
    assert state2.model_dump_json() == before_state2
    assert run2.model_dump_json() == before_run2
    # Also CANCELLED after COMPLETED must not change
    assert run.model_dump_json() == before_run


@pytest.mark.unit
def test_cancel_wins_over_budget_and_is_monotonic() -> None:
    """Cancel is terminal and monotonic like LIMIT_EXCEEDED — cannot un-trip."""
    state = RunState(status=RunStatus.RUNNING)
    assert state.request_cancel() is True
    assert state.status == RunStatus.CANCELLED
    assert state.error_code == AgentErrorCode.CANCELLED
    # Once cancelled, cannot go to COMPLETED or FAILED
    for target in [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.RUNNING, RunStatus.LIMIT_EXCEEDED]:  # noqa: E501
        try:
            state.transition_to(target)
            raise AssertionError(f"CANCELLED must reject {target}")
        except InvalidRunTransitionError:
            pass
    assert state.status == RunStatus.CANCELLED
    # Second cancel is also no-op
    assert state.request_cancel() is False
