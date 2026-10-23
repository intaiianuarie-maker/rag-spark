"""T-A14 / V3-03 — bind approval to runtime authority.

Astra CONFIRMED Med: the approve branch only re-queued the run; the runtime
``approve_run`` authority was never called, so a restored run re-checked
``_is_approved(run, principal, tool, policy_revision)`` → false, stayed
``WAITING_APPROVAL`` and looped QUEUED → WAITING forever.

Controls covered here:

* **approve → real resume → one intended dispatch** — the approve route binds
  the approval server-side, so the runtime actually dispatches the write tool
  exactly once (and only the named tool).
* **stale-policy refusal** — an action raised at ``rev1`` cannot be approved
  once the run sits at ``rev2`` (409, no approval recorded, no dispatch).
* **wrong-action refusal** — approving a tool that is not the pending one is
  refused (409, no approval recorded).
* **approver authority** — a principal who does not own the run cannot approve
  it (403).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from agents.interfaces import (
    ModelPolicy,
    RunBudget,
    RunContext,
    RunState,
    RunStatus,
)
from agents.policy import WorkspaceAgentPolicy, reset_workspace_policies, set_workspace_policy
from agents.runtime import (
    _APPROVALS,
    _WAITING,
    AgentRuntime,
)
from agents.tools import Tool, ToolRegistry
from app.agents.lifecycle_routes import router as lifecycle_router
from app.authz import Action, Resource
from app.authz.principal import get_principal
from core.auth import Principal, Role
from fastapi import FastAPI
from fastapi.testclient import TestClient
from models.interfaces import GenerationRequest, GenerationResult, LLMProvider
from stores.agent_stores import DurableRunRecord, get_run_store, reset_all_stores

pytestmark = pytest.mark.unit

WS_ID_1 = "11111111-1111-1111-1111-111111111111"
WS_ID_2 = "22222222-2222-2222-2222-222222222222"
USER_1 = "user-a14"
OTHER_USER = "user-a14-other"


class _ScriptedLLM(LLMProvider):
    """Deterministic LLM that replays a fixed script."""

    provider_name = "scripted"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        text = self.responses.pop(0) if self.responses else "Final Answer: done"
        return GenerationResult(
            text=text,
            model=request.model,
            finish_reason="stop",
            prompt_tokens=10,
            completion_tokens=5,
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        yield ""


def _spy_write_tool(name: str, calls: list[dict]) -> Tool:
    class _SpyWriteTool(Tool):
        description = "spy write tool"
        input_schema: ClassVar[dict] = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "additionalProperties": False,
        }

        async def execute(self, workspace_id: str, **kwargs):
            calls.append(dict(kwargs))
            return {"ok": True}

    _SpyWriteTool.name = name  # type: ignore[attr-defined]
    return _SpyWriteTool()


def _build_test_client(monkeypatch: pytest.MonkeyPatch, user_id: str = USER_1) -> TestClient:
    app = FastAPI()
    app.include_router(lifecycle_router)

    def _mock_require(principal: Principal, action: Action, resource: Resource) -> None:
        ws = resource.workspace_id
        if ws and ws not in principal.workspace_roles:
            from app.authz.errors import ApiError

            raise ApiError(status_code=403, code="workspace_forbidden", message="Forbidden")

    monkeypatch.setattr("app.agents.lifecycle_routes.require", _mock_require)

    async def _mock_principal() -> Principal:
        return Principal(
            user_id=user_id,
            email=f"{user_id}@example.com",
            is_authenticated=True,
            workspace_roles={WS_ID_1: Role.ADMIN},
        )

    app.dependency_overrides[get_principal] = _mock_principal
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _isolate_env() -> Iterator[None]:
    _APPROVALS.clear()
    _WAITING.clear()
    reset_workspace_policies()
    reset_all_stores()
    yield
    _APPROVALS.clear()
    _WAITING.clear()
    reset_workspace_policies()
    reset_all_stores()


def _context(run_id: str, *, revision: str = "rev1", principal_id: str = USER_1) -> RunContext:
    return RunContext(
        run_id=run_id,
        workspace_id=WS_ID_1,
        principal_id=principal_id,
        authz_policy_revision=revision,
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(max_steps=5, max_tool_calls=5, max_tokens=100000),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        allowed_tools=["publish"],
    )


def _persist_waiting_run(run_id: str, ctx: RunContext) -> None:
    """Persist a run in WAITING_APPROVAL the way the runtime leaves it."""
    now_iso = datetime.now(UTC).isoformat()
    get_run_store().create_run(
        DurableRunRecord(
            run_id=run_id,
            workspace_id=ctx.workspace_id,
            principal_id=ctx.principal_id,
            goal="needs approval",
            status=RunStatus.WAITING_APPROVAL,
            context=ctx,
            state=RunState(status=RunStatus.WAITING_APPROVAL),
            created_at=now_iso,
            updated_at=now_iso,
        )
    )


def _park_on_publish(
    run_id: str, *, revision: str = "rev1", principal_id: str = USER_1
) -> RunContext:
    """Run the real runtime until it parks on the ``publish`` WRITE tool."""
    ctx = _context(run_id, revision=revision, principal_id=principal_id)
    calls: list[dict] = []
    registry = ToolRegistry()
    registry.register(_spy_write_tool("publish", calls))
    llm = _ScriptedLLM(
        [
            'Thought: try write\nAction: publish\nAction Input: {"x": 1}',
            "Final Answer: done",
        ]
    )
    runtime = AgentRuntime(llm, registry)
    asyncio.run(runtime.run_enforced(context=ctx, goal="park"))
    assert calls == [], "unapproved WRITE must never execute"
    # The pending tuple is the authority the approve route resolves against.
    # (Legacy AgentRunStatus maps WAITING_APPROVAL to RUNNING, so assert the
    # durable pending tuple rather than the legacy enum.)
    assert run_id in _WAITING
    _persist_waiting_run(run_id, ctx)
    return ctx


# ---------------------------------------------------------------------------
# Metric: approve → real resume → one intended dispatch
# ---------------------------------------------------------------------------


def test_approve_binds_runtime_authority_and_dispatches_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The approve route records the runtime approval, so resume dispatches once."""
    set_workspace_policy(
        WS_ID_1, WorkspaceAgentPolicy(workspace_id=WS_ID_1, allowed_tools=("publish",))
    )
    run_id = str(uuid.uuid4())
    ctx = _park_on_publish(run_id)

    # Before approval: not approved → no dispatch possible.
    assert (run_id, USER_1, "publish") not in _APPROVALS

    client = _build_test_client(monkeypatch, user_id=USER_1)
    resp = client.post(
        f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id}/approval",
        json={"decision": "approved", "tool_name": "publish"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "approved"
    assert resp.json()["status"] == "queued"

    # The runtime authority is now bound server-side (run, principal, tool, rev).
    assert _APPROVALS[(run_id, USER_1, "publish")] == ctx.authz_policy_revision

    # Real resume: exactly one intended dispatch of the approved tool.
    calls: list[dict] = []
    registry = ToolRegistry()
    registry.register(_spy_write_tool("publish", calls))
    runtime = AgentRuntime(_ScriptedLLM(["Final Answer: done after resume"]), registry)
    asyncio.run(runtime.run_enforced(context=ctx, goal="resume"))
    assert len(calls) == 1, f"approved WRITE must dispatch exactly once, got {len(calls)}"


# ---------------------------------------------------------------------------
# Metric: stale-policy refusal
# ---------------------------------------------------------------------------


def test_stale_policy_approval_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An action raised at rev1 cannot be approved once the run is at rev2."""
    set_workspace_policy(
        WS_ID_1, WorkspaceAgentPolicy(workspace_id=WS_ID_1, allowed_tools=("publish",))
    )
    run_id = str(uuid.uuid4())
    # Parked at rev1 …
    _park_on_publish(run_id, revision="rev1")
    # … but the run is now operating under a newer policy revision.
    record = get_run_store().get_run(run_id)
    assert record is not None and record.context is not None
    stale_ctx = record.context.model_copy(update={"authz_policy_revision": "rev2"})
    record.context = stale_ctx
    get_run_store().update_run(record)

    client = _build_test_client(monkeypatch, user_id=USER_1)
    resp = client.post(
        f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id}/approval",
        json={"decision": "approved", "tool_name": "publish"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "approval_policy_stale"
    # No approval recorded — a stale binding could never pass the runtime re-check.
    assert (run_id, USER_1, "publish") not in _APPROVALS


# ---------------------------------------------------------------------------
# Metric: wrong-action refusal
# ---------------------------------------------------------------------------


def test_wrong_action_approval_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approving a tool that is not the pending action is refused."""
    set_workspace_policy(
        WS_ID_1,
        WorkspaceAgentPolicy(workspace_id=WS_ID_1, allowed_tools=("publish", "delete_all")),
    )
    run_id = str(uuid.uuid4())
    _park_on_publish(run_id)

    client = _build_test_client(monkeypatch, user_id=USER_1)
    resp = client.post(
        f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id}/approval",
        json={"decision": "approved", "tool_name": "delete_all"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "approval_action_mismatch"
    assert (run_id, USER_1, "publish") not in _APPROVALS
    assert (run_id, USER_1, "delete_all") not in _APPROVALS


# ---------------------------------------------------------------------------
# Approver authority
# ---------------------------------------------------------------------------


def test_non_owner_approver_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A principal who does not own the run cannot approve its action."""
    set_workspace_policy(
        WS_ID_1, WorkspaceAgentPolicy(workspace_id=WS_ID_1, allowed_tools=("publish",))
    )
    run_id = str(uuid.uuid4())
    _park_on_publish(run_id)

    # Approver is a different principal than the run owner.
    client = _build_test_client(monkeypatch, user_id=OTHER_USER)
    resp = client.post(
        f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id}/approval",
        json={"decision": "approved", "tool_name": "publish"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "approval_authority_denied"
    assert (run_id, USER_1, "publish") not in _APPROVALS


# ---------------------------------------------------------------------------
# No phantom approval when no pending action is visible in this process
# ---------------------------------------------------------------------------


def test_no_pending_action_writes_no_phantom_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no pending tuple (API replica), the route re-queues without binding."""
    set_workspace_policy(
        WS_ID_1, WorkspaceAgentPolicy(workspace_id=WS_ID_1, allowed_tools=("publish",))
    )
    run_id = str(uuid.uuid4())
    ctx = _context(run_id)
    _persist_waiting_run(run_id, ctx)
    assert run_id not in _WAITING

    client = _build_test_client(monkeypatch, user_id=USER_1)
    resp = client.post(
        f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id}/approval",
        json={"decision": "approved", "tool_name": "publish"},
    )
    assert resp.status_code == 200
    # The response discloses that no binding was made, and none was invented.
    assert resp.json()["error"] == "approval_binding_unavailable"
    assert _APPROVALS == {}


# ---------------------------------------------------------------------------
# Cross-workspace isolation still holds
# ---------------------------------------------------------------------------


def test_cross_workspace_approval_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approving from another workspace stays 403 (unchanged isolation)."""
    set_workspace_policy(
        WS_ID_1, WorkspaceAgentPolicy(workspace_id=WS_ID_1, allowed_tools=("publish",))
    )
    run_id = str(uuid.uuid4())
    _park_on_publish(run_id)

    client = _build_test_client(monkeypatch, user_id=USER_1)
    resp = client.post(
        f"/v1/workspaces/{WS_ID_2}/agent/runs/{run_id}/approval",
        json={"decision": "approved", "tool_name": "publish"},
    )
    assert resp.status_code == 403
    assert (run_id, USER_1, "publish") not in _APPROVALS