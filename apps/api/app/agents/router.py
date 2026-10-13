"""Agent run lifecycle over HTTP — start/status/cancel (T-V3-021).

Thin HTTP over the AgentRuntime (packages/agents/runtime.py).
Every gate enforced identically to the in-process path: budget reservation,
approval gates, scoped workspace, bounded everything. The API must not be
a gate bypass.

Wire: behind the current JWT/workspace middleware (get_principal + require),
no new auth mechanism. AuthNZ reuses the existing stack.

Seam: create_runtime() is the factory tests patch (like search's
create_orchestrator) so unit tests run on fakes without a live stack.

Storage: in-memory dict for the skeleton (same as runtime's _PERSISTED_*
stores). Production would use the StepStore/DB seam.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from agents.interfaces import AgentErrorCode, ModelPolicy, RunBudget, RunContext, RunStatus
from agents.runtime import AgentRuntime
from agents.tools import CalculatorTool, ToolRegistry
from core.auth import Principal
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.authz import Action, Resource, require
from app.authz.principal import get_principal

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/agent", tags=["agent"])


# ---------------------------------------------------------------------------
# In-memory run store (skeleton — mirrors runtime's _PERSISTED_* )
# ---------------------------------------------------------------------------

_RUNS: dict[str, dict[str, Any]] = {}


def _clear_store() -> None:
    _RUNS.clear()


def create_runtime() -> AgentRuntime:
    """Factory seam — tests monkeypatch this (like search.create_orchestrator)."""
    # Production would wire the real LLM provider via models router;
    # skeleton uses a no-op LLM that abstains so gate checks still run.
    from collections.abc import AsyncIterator

    from models.interfaces import (
        GenerationRequest,
        GenerationResult,
        LLMProvider,
    )

    class _NoopLLM(LLMProvider):
        provider_name = "noop"

        async def generate(self, request: GenerationRequest) -> GenerationResult:
            return GenerationResult(text="Final Answer: done", model=request.model, prompt_tokens=1, completion_tokens=1)  # noqa: E501

        async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
            yield ""

    reg = ToolRegistry()
    reg.register(CalculatorTool())
    llm = _NoopLLM()
    return AgentRuntime(llm, reg)


class StartRunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=4096)
    max_steps: int = Field(default=5, ge=1, le=20)
    max_tokens: int = Field(default=100000, ge=1)
    max_tool_calls: int = Field(default=50, ge=0)
    allowed_tools: list[str] = Field(default_factory=list)


class StartRunResponse(BaseModel):
    run_id: str
    workspace_id: str
    status: str
    error: str | None = None
    error_code: str | None = None


class StatusResponse(BaseModel):
    run_id: str
    workspace_id: str
    status: str
    error: str | None = None
    error_code: str | None = None


def _http_error(exc):
    from core.errors import error_detail

    from app.authz.errors import ApiError

    if isinstance(exc, ApiError):
        from fastapi import HTTPException

        return HTTPException(status_code=exc.status_code, detail=error_detail(exc.code, exc.message))  # noqa: E501
    raise exc from exc


def _require_workspace(principal: Principal, workspace_id: uuid.UUID) -> None:
    from app.authz.errors import ApiError

    try:
        require(principal, Action.DOCUMENT_READ, Resource(workspace_id=str(workspace_id)))
    except ApiError as exc:
        raise _http_error(exc) from exc


@router.post("/runs", response_model=StartRunResponse, status_code=status.HTTP_201_CREATED)
async def start_run(
    workspace_id: uuid.UUID,
    body: StartRunRequest,
    principal: Principal = Depends(get_principal),
) -> StartRunResponse:
    _require_workspace(principal, workspace_id)

    # Bounded everything — RunBudget validates bounds, RunContext validates IDs
    try:
        budget = RunBudget(max_steps=body.max_steps, max_tokens=body.max_tokens, max_tool_calls=body.max_tool_calls)  # noqa: E501
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "invalid_budget", "message": str(exc)}) from exc  # noqa: E501

    run_id = str(uuid.uuid4())
    # Reuse same gates as in-process: budget reservation, approval, workspace scope, bounded.
    # Build RunContext with workspace_id from URL (not body) — cross-workspace bypass impossible.
    try:
        ctx = RunContext(
            run_id=run_id,
            workspace_id=str(workspace_id),
            principal_id=principal.user_id,
            authz_policy_revision="rev1",
            model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
            budget=budget,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            allowed_tools=body.allowed_tools,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "invalid_context", "message": str(exc)}) from exc  # noqa: E501

    runtime = create_runtime()
    # Run through the SAME runtime path as in-process — parity by construction.
    # The runtime enforces budget reservation, approval gates, tool allowlist, etc.
    from agents.runtime import _PERSISTED_RUNS, _PERSISTED_STATES

    # Clear any stale state for this run_id (test isolation)
    _PERSISTED_RUNS.pop(run_id, None)
    _PERSISTED_STATES.pop(run_id, None)

    agent_run = await runtime.run_enforced(context=ctx, goal=body.goal)

    # Read the persisted RunState for the canonical status/error_code
    state = _PERSISTED_STATES.get(run_id)
    run_status = state.status.value if state else agent_run.status.value
    error_code = state.error_code.value if state and state.error_code else None
    error_detail = state.error_detail if state else agent_run.error

    # Store for status/cancel
    _RUNS[run_id] = {
        "run_id": run_id,
        "workspace_id": str(workspace_id),
        "principal_id": principal.user_id,
        "status": run_status,
        "error": error_detail,
        "error_code": error_code,
        "goal": body.goal,
    }

    # Map terminal gate failures to HTTP status that still proves BLOCKED (never 200 for bypass)
    # For parity, we return 201 with the run's terminal status — the gate is visible in the body.
    # Callers that poll status see the same LIMIT_EXCEEDED / WAITING_APPROVAL as in-process.
    return StartRunResponse(run_id=run_id, workspace_id=str(workspace_id), status=run_status, error=error_detail, error_code=error_code)  # noqa: E501


@router.get("/runs/{run_id}", response_model=StatusResponse)
async def get_run_status(
    workspace_id: uuid.UUID,
    run_id: str,
    principal: Principal = Depends(get_principal),
) -> StatusResponse:
    _require_workspace(principal, workspace_id)
    rec = _RUNS.get(run_id)
    if rec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found", "message": "run not found"})  # noqa: E501
    # Scoped workspace — cross-workspace attempt BLOCKED
    if rec["workspace_id"] != str(workspace_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "workspace_mismatch", "message": "run belongs to a different workspace"})  # noqa: E501
    # Also check principal still has access to the run's workspace (defense in depth)
    if rec["principal_id"] != principal.user_id:
        # If principal is not the owner, still check workspace membership via require already done,
        # but cross-user within same workspace is allowed to poll (read). Keep 200.
        pass
    return StatusResponse(
        run_id=run_id, workspace_id=rec["workspace_id"], status=rec["status"], error=rec["error"], error_code=rec["error_code"]  # noqa: E501
    )


@router.post("/runs/{run_id}/cancel", response_model=StatusResponse)
async def cancel_run(
    workspace_id: uuid.UUID,
    run_id: str,
    principal: Principal = Depends(get_principal),
) -> StatusResponse:
    _require_workspace(principal, workspace_id)
    rec = _RUNS.get(run_id)
    if rec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found", "message": "run not found"})  # noqa: E501
    if rec["workspace_id"] != str(workspace_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "workspace_mismatch", "message": "run belongs to a different workspace"})  # noqa: E501
    # Cancel via same RunState gate as in-process
    from agents.runtime import _PERSISTED_STATES

    state = _PERSISTED_STATES.get(run_id)
    if state is not None:
        try:
            state.request_cancel()
            rec["status"] = state.status.value
            rec["error_code"] = state.error_code.value if state.error_code else None
            rec["error"] = state.error_detail
        except Exception:
            pass
    else:
        # If no persisted state (e.g., noop run already terminal), mark cancelled if still active
        if rec["status"] in (RunStatus.QUEUED.value, RunStatus.RUNNING.value, RunStatus.WAITING_APPROVAL.value, RunStatus.WAITING_INPUT.value):  # noqa: E501
            rec["status"] = RunStatus.CANCELLED.value
            rec["error_code"] = AgentErrorCode.CANCELLED.value
            rec["error"] = "run cancelled"

    return StatusResponse(
        run_id=run_id, workspace_id=rec["workspace_id"], status=rec["status"], error=rec["error"], error_code=rec["error_code"]  # noqa: E501
    )
