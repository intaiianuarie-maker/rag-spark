"""Tests for V3 Real Factories, Server Policy, and Per-Run Budget Enforcement (T-R14).

Proves:
1. Widening requests honored: 0
   - Above-policy requests strictly refused (PolicyViolationError, HTTP 400 Bad Request)
     across steps, tokens, tool_calls, costs, models, and tools.
   - Below-policy requests honored (narrowing limits).
2. Unconfigured paths changed: 0
   - create_agent_runtime raises AgentUnavailableError when unconfigured.
   - Zero tools registered and zero calls made on unconfigured paths.
   - R02 contract untouched.
3. Budget overruns silent: 0
   - Tool costs tracked per-run in Usage (tool_cost_usd, cost_usd).
   - Tool budget cap trips loudly with typed error (AgentErrorCode.BUDGET_EXCEEDED).
   - Error detail explicitly states the spend and cap amount.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from agents.factories import create_agent_runtime, resolve_agent_llm_provider
from agents.interfaces import (
    AgentErrorCode,
    ModelPolicy,
    RunBudget,
    RunContext,
    RunStatus,
    Usage,
)
from agents.policy import (
    PolicyViolationError,
    WorkspaceAgentPolicy,
    derive_effective_run_config,
    reset_workspace_policies,
    set_workspace_policy,
)
from agents.runtime import AgentRuntime, can_reserve_tool_spend
from agents.tools import Tool, ToolRegistry
from agents.unavailable import MISSING_LLM_PROVIDER, AgentUnavailableError
from app.main import app
from fastapi.testclient import TestClient
from models.interfaces import (
    GenerationRequest,
    GenerationResult,
    LLMProvider,
)

WS_ID = "00000000-0000-0000-0000-000000000001"


# ============================================================================
# Helpers & Mocks
# ============================================================================


class MockLLM(LLMProvider):
    """Deterministic LLM for testing tool calling and completion."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls.append(request)
        if self.responses:
            text = self.responses.pop(0)
        else:
            text = "Final Answer: completed"
        return GenerationResult(
            text=text,
            model=request.model,
            prompt_tokens=10,
            completion_tokens=5,
        )

    async def stream(self, request: GenerationRequest):
        yield ""



class CostlyTool(Tool):
    """Tool with explicit cost for budget tracking tests."""

    name: ClassVar[str] = "costly_tool"
    description: ClassVar[str] = "Tool with a known per-invocation cost"
    cost_usd: float = 0.05
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"arg": {"type": "string"}},
        "required": [],
        "additionalProperties": False,
    }

    def __init__(self, cost_usd: float = 0.05) -> None:
        self.cost_usd = cost_usd
        self.invocations: int = 0

    async def execute(self, workspace_id: str, **kwargs: Any) -> Any:
        self.invocations += 1
        return {"invocations": self.invocations, "cost": self.cost_usd}


# ============================================================================
# 1. Policy Derivation & Request Narrowing / Widening Tests
# ============================================================================


def test_policy_derivation_narrows_limits_successfully() -> None:
    """Request may narrow limits (steps, tokens, tool_calls, costs, tools, model)."""
    policy = WorkspaceAgentPolicy(
        workspace_id=WS_ID,
        max_steps=10,
        max_tokens=50000,
        max_tool_calls=20,
        max_cost_usd=1.00,
        max_tool_cost_usd=0.50,
        max_wall_clock_s=120.0,
        allowed_models=("google/gemma-4-26b-a4b-it:free", "openai/gpt-4o-mini"),
        default_model="google/gemma-4-26b-a4b-it:free",
        allowed_tools=("calculator", "knowledge_search", "knowledge_read"),
    )

    effective = derive_effective_run_config(
        policy,
        goal="calculate 2+2",
        requested_model="openai/gpt-4o-mini",
        requested_tools=["calculator"],
        requested_steps=4,
        requested_tokens=10000,
        requested_tool_calls=5,
        requested_cost_usd=0.25,
        requested_tool_cost_usd=0.10,
        requested_wall_clock_s=60.0,
    )

    assert effective.model == "openai/gpt-4o-mini"
    assert effective.allowed_tools == ["calculator"]
    assert effective.max_steps == 4
    assert effective.max_tokens == 10000
    assert effective.max_tool_calls == 5
    assert effective.max_cost_usd == 0.25
    assert effective.max_tool_cost_usd == 0.10
    assert effective.max_wall_clock_s == 60.0


def test_policy_derivation_defaults_to_org_policy_when_unspecified() -> None:
    """When request does not specify limits, server policy limits are used."""
    policy = WorkspaceAgentPolicy(workspace_id=WS_ID)
    effective = derive_effective_run_config(policy, goal="search docs")

    assert effective.model == policy.default_model
    assert effective.allowed_tools == list(policy.allowed_tools)
    assert effective.max_steps == policy.max_steps
    assert effective.max_tokens == policy.max_tokens
    assert effective.max_tool_calls == policy.max_tool_calls
    assert effective.max_cost_usd == policy.max_cost_usd
    assert effective.max_tool_cost_usd == policy.max_tool_cost_usd


@pytest.mark.parametrize(
    ("kwargs", "expected_field"),
    [
        ({"requested_steps": 15}, "max_steps"),
        ({"requested_tokens": 150_000}, "max_tokens"),
        ({"requested_tool_calls": 60}, "max_tool_calls"),
        ({"requested_cost_usd": 2.50}, "max_cost_usd"),
        ({"requested_tool_cost_usd": 0.80}, "max_tool_cost_usd"),
        ({"requested_wall_clock_s": 500.0}, "max_wall_clock_s"),
        ({"requested_tools": ["unauthorized_exec_tool"]}, "allowed_tools"),
        ({"requested_model": "unapproved-mega-llm"}, "model"),
    ],
)
def test_policy_derivation_refuses_above_policy_widening(
    kwargs: dict[str, Any], expected_field: str
) -> None:
    """Metric: widening requests honored: 0. Every widening attempt raises PolicyViolationError."""
    policy = WorkspaceAgentPolicy(
        workspace_id=WS_ID,
        max_steps=10,
        max_tokens=100_000,
        max_tool_calls=50,
        max_cost_usd=1.00,
        max_tool_cost_usd=0.50,
        max_wall_clock_s=300.0,
        allowed_models=("google/gemma-4-26b-a4b-it:free",),
        allowed_tools=("calculator", "knowledge_search", "knowledge_read"),
    )

    with pytest.raises(PolicyViolationError) as exc_info:
        derive_effective_run_config(policy, goal="test", **kwargs)

    assert exc_info.value.field == expected_field
    assert "exceeds" in str(exc_info.value) or "not allowed" in str(exc_info.value)


# ============================================================================
# 2. HTTP API Policy Enforcement Tests (Widening = 400 Bad Request)
# ============================================================================


def _api_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from core.auth import Principal

    async def _mock_principal():
        return Principal(user_id="user-1", email="test@example.com")

    from app.authz.principal import get_principal

    app.dependency_overrides[get_principal] = _mock_principal
    monkeypatch.setattr("app.agents.router.require", lambda *args, **kwargs: None)
    return TestClient(app)


def test_api_widening_steps_refused_with_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widening max_steps beyond policy is refused with HTTP 400."""
    reset_workspace_policies()
    set_workspace_policy(WS_ID, WorkspaceAgentPolicy(workspace_id=WS_ID, max_steps=5))
    client = _api_client(monkeypatch)

    resp = client.post(
        f"/v1/workspaces/{WS_ID}/agent/runs",
        json={"goal": "test goal", "max_steps": 10},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["detail"]["error"] == "policy_violation"
    assert body["detail"]["field"] == "max_steps"


def test_api_widening_tools_refused_with_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requesting tools not in workspace policy is refused with HTTP 400."""
    reset_workspace_policies()
    set_workspace_policy(
        WS_ID,
        WorkspaceAgentPolicy(
            workspace_id=WS_ID,
            allowed_tools=("calculator",),
        ),
    )
    client = _api_client(monkeypatch)

    resp = client.post(
        f"/v1/workspaces/{WS_ID}/agent/runs",
        json={"goal": "test goal", "allowed_tools": ["calculator", "knowledge_search"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["detail"]["error"] == "policy_violation"
    assert body["detail"]["field"] == "allowed_tools"


def test_api_widening_model_refused_with_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requesting a model not in workspace policy is refused with HTTP 400."""
    reset_workspace_policies()
    set_workspace_policy(
        WS_ID,
        WorkspaceAgentPolicy(
            workspace_id=WS_ID,
            allowed_models=("google/gemma-4-26b-a4b-it:free",),
        ),
    )
    client = _api_client(monkeypatch)

    resp = client.post(
        f"/v1/workspaces/{WS_ID}/agent/runs",
        json={"goal": "test goal", "model": "unapproved-vendor/gpt-5"},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["detail"]["error"] == "policy_violation"
    assert body["detail"]["field"] == "model"


# ============================================================================
# 3. Per-Run Tool Budget Enforcement Tests (Loud Cap Trip with Stated Spend)
# ============================================================================


def test_can_reserve_tool_spend_boundary_logic() -> None:
    """Test pure reservation boundary checks for tool spend."""
    budget = RunBudget(max_tool_cost_usd=0.10, max_cost_usd=0.20)

    # None usage is strictly DENY
    assert not can_reserve_tool_spend(None, budget, 0.01)

    # Negative cost is invalid
    usage = Usage(tool_cost_usd=0.00, cost_usd=0.00)
    assert not can_reserve_tool_spend(usage, budget, -0.01)

    # Initial spend within budget
    assert can_reserve_tool_spend(usage, budget, 0.05)
    assert can_reserve_tool_spend(usage, budget, 0.10)
    assert not can_reserve_tool_spend(usage, budget, 0.11)

    # Existing tool spend accumulated
    usage_spent = Usage(tool_cost_usd=0.08, cost_usd=0.08)
    assert can_reserve_tool_spend(usage_spent, budget, 0.02)
    assert not can_reserve_tool_spend(usage_spent, budget, 0.03)

    # Total cost cap binding
    usage_total = Usage(tool_cost_usd=0.05, cost_usd=0.18)
    assert can_reserve_tool_spend(usage_total, budget, 0.02)
    assert not can_reserve_tool_spend(usage_total, budget, 0.03)


@pytest.mark.asyncio
async def test_tool_budget_trips_loudly_with_spend_stated() -> None:
    """Metric: budget overruns silent: 0.

    When tool spend would exceed max_tool_cost_usd, the run transitions to
    LIMIT_EXCEEDED with AgentErrorCode.BUDGET_EXCEEDED and error_detail states
    the spend and cap amounts loudly.
    """
    tool = CostlyTool(cost_usd=0.05)
    registry = ToolRegistry()
    registry.register(tool)

    # Mock LLM calls the costly tool twice, then gives final answer
    llm = MockLLM(
        responses=[
            'Action: costly_tool\nAction Input: {"arg": "call-1"}',
            'Action: costly_tool\nAction Input: {"arg": "call-2"}',
            "Final Answer: should never be reached",
        ]
    )

    runtime = AgentRuntime(llm, registry)

    # Tool cap is $0.08: call 1 ($0.05) succeeds; call 2 ($0.05 + $0.05 = $0.10) exceeds cap
    budget = RunBudget(
        max_steps=5,
        max_tokens=10000,
        max_tool_calls=10,
        max_tool_cost_usd=0.08,
        max_cost_usd=1.00,
    )
    context = RunContext(
        run_id=str(uuid.uuid4()),
        workspace_id=WS_ID,
        principal_id="user-1",
        authz_policy_revision="rev1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=budget,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["costly_tool"],
    )

    _ = await runtime.run_enforced(context=context, goal="run costly tools")

    # Assert run terminated at LIMIT_EXCEEDED loudly
    from agents.runtime import _PERSISTED_STATES, _PERSISTED_USAGES

    state = _PERSISTED_STATES[context.run_id]
    usage = _PERSISTED_USAGES[context.run_id]

    assert state.status == RunStatus.LIMIT_EXCEEDED
    assert state.error_code == AgentErrorCode.BUDGET_EXCEEDED

    # Spend must be explicitly stated in the error detail
    assert state.error_detail is not None
    assert "tool budget would be exceeded" in state.error_detail
    assert "0.1000" in state.error_detail or "$0.10" in state.error_detail
    assert "0.0800" in state.error_detail or "$0.08" in state.error_detail

    # Only 1 invocation actually completed
    assert tool.invocations == 1
    assert usage.tool_cost_usd == 0.05
    assert usage.cost_usd == 0.05


@pytest.mark.asyncio
async def test_total_cost_cap_trips_loudly_before_first_tool_dispatch() -> None:
    """When a single tool's cost exceeds the total cost cap, it trips before dispatch."""
    tool = CostlyTool(cost_usd=0.25)
    registry = ToolRegistry()
    registry.register(tool)

    llm = MockLLM(responses=['Action: costly_tool\nAction Input: {"arg": "big-spend"}'])
    runtime = AgentRuntime(llm, registry)

    budget = RunBudget(
        max_steps=5,
        max_tokens=10000,
        max_tool_calls=10,
        max_cost_usd=0.20,  # Cap $0.20 < tool cost $0.25
    )
    context = RunContext(
        run_id=str(uuid.uuid4()),
        workspace_id=WS_ID,
        principal_id="user-1",
        authz_policy_revision="rev1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=budget,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["costly_tool"],
    )

    _ = await runtime.run_enforced(context=context, goal="run single big spend")

    from agents.runtime import _PERSISTED_STATES

    state = _PERSISTED_STATES[context.run_id]
    assert state.status == RunStatus.LIMIT_EXCEEDED
    assert state.error_code == AgentErrorCode.BUDGET_EXCEEDED
    assert state.error_detail is not None
    assert "0.2500" in state.error_detail or "$0.25" in state.error_detail
    assert "0.2000" in state.error_detail or "$0.20" in state.error_detail
    assert tool.invocations == 0


# ============================================================================
# 4. Factories & Standard Tools Wiring Tests
# ============================================================================


def test_factory_unconfigured_refusal_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Metric: unconfigured paths changed: 0.

    resolve_agent_llm_provider returns None and create_agent_runtime raises
    AgentUnavailableError when unconfigured.
    """
    for key in ("LITELLM_URL", "LITELLM_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    provider = resolve_agent_llm_provider()
    assert provider is None

    with pytest.raises(AgentUnavailableError) as exc_info:
        create_agent_runtime()

    assert exc_info.value.missing == MISSING_LLM_PROVIDER


def test_factory_configured_wires_standard_tools() -> None:
    """When configured with LLM, create_agent_runtime registers standard tools with costs."""
    fake_llm = MockLLM([])
    runtime = create_agent_runtime(llm=fake_llm)

    tools = {t.name: t for t in runtime.registry.list_tools()}

    # Standard V3 tools registered
    assert "calculator" in tools
    assert "knowledge_search" in tools
    assert "knowledge_read" in tools

    # Costs configured on tools
    calc = tools["calculator"]
    search = tools["knowledge_search"]
    read = tools["knowledge_read"]

    assert calc.cost_usd == 0.001
    assert search.cost_usd == 0.01
    assert read.cost_usd == 0.005
