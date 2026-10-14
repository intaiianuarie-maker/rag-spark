"""Agent Run Durable Lifecycle HTTP Routes (T-R15).

Provides HTTP endpoints enforcing server policy:
1. POST /runs/queued (and /runs/async):
   - Pre-allocates durable run ID BEFORE returning 202 Accepted.
   - Enforces server-side workspace policy (widening rejected with 400).
   - Writes queued record to RunStore first, enqueues to durable queue second,
     returns 202 Accepted third (runs accepted without pre-allocated ID = 0).
2. GET /runs/{run_id}/status:
   - Scoped workspace status lookup. Cross-workspace returns 403.
3. GET /runs/{run_id}/history:
   - Returns ordered StepEvent audit trail from StepStore.
4. POST /runs/{run_id}/cancel:
   - Server-policy enforced cancellation (monotonic no-op if already terminal).
5. POST /runs/{run_id}/approval (and /approve):
   - Server-policy enforced approval: rejects disallowed tools and non-waiting runs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from agents.interfaces import (
    AgentErrorCode,
    ModelPolicy,
    RunBudget,
    RunContext,
    RunState,
    RunStatus,
)
from agents.policy import (
    PolicyViolationError,
    derive_effective_run_config,
    get_workspace_policy,
)
from core.auth import Principal
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from stores.agent_stores import (
    DurableRunRecord,
    get_queue,
    get_run_store,
    get_step_store,
)

from app.authz import Action, Resource, require
from app.authz.principal import get_principal

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/agent", tags=["agent-lifecycle"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class StartQueuedRunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=4096)
    model: str | None = Field(
        default=None, description="Requested model (must be allowed by policy)"
    )
    max_steps: int | None = Field(default=None, ge=1, le=20)
    max_tokens: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0.0)
    max_tool_cost_usd: float | None = Field(default=None, ge=0.0)
    max_wall_clock_s: float | None = Field(default=None, ge=1.0)
    allowed_tools: list[str] = Field(default_factory=list)


class StartQueuedRunResponse(BaseModel):
    run_id: str
    workspace_id: str
    status: str = "queued"
    created_at: str


class StatusResponse(BaseModel):
    run_id: str
    workspace_id: str
    status: str
    error: str | None = None
    error_code: str | None = None
    created_at: str
    updated_at: str


class HistoryResponse(BaseModel):
    run_id: str
    workspace_id: str
    steps: list[dict[str, Any]]
    total_steps: int


class CancelResponse(BaseModel):
    run_id: str
    workspace_id: str
    status: str
    cancelled: bool
    error: str | None = None
    error_code: str | None = None


class ApprovalRequest(BaseModel):
    decision: str = Field(description="'approved' or 'rejected'")
    tool_name: str | None = Field(default=None, description="Optional tool name being approved")
    feedback: str | None = None


class ApprovalResponse(BaseModel):
    run_id: str
    workspace_id: str
    status: str
    decision: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Auth Helper
# ---------------------------------------------------------------------------


def _http_error(exc: Exception) -> HTTPException:
    from core.errors import error_detail

    from app.authz.errors import ApiError

    if isinstance(exc, ApiError):
        return HTTPException(
            status_code=exc.status_code,
            detail=error_detail(exc.code, exc.message),
        )
    raise exc


def _require_workspace(principal: Principal, workspace_id: uuid.UUID) -> None:
    from app.authz.errors import ApiError

    try:
        require(principal, Action.DOCUMENT_READ, Resource(workspace_id=str(workspace_id)))
    except ApiError as exc:
        raise _http_error(exc) from exc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/runs/queued",
    response_model=StartQueuedRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@router.post(
    "/runs/async",
    response_model=StartQueuedRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_queued_run(
    workspace_id: uuid.UUID,
    body: StartQueuedRunRequest,
    principal: Principal = Depends(get_principal),
) -> StartQueuedRunResponse:
    """Accept and queue an agent run.

    Guarantees:
    - Pre-allocated durable run ID persisted before HTTP 202 is returned.
    - Server policy derived and strictly enforced (widening = 400).
    - Queued job enqueued to durable queue.
    """
    _require_workspace(principal, workspace_id)

    ws_id_str = str(workspace_id)
    policy = get_workspace_policy(ws_id_str)

    # 1. Enforce server-side policy derivation
    try:
        effective_cfg = derive_effective_run_config(
            policy,
            goal=body.goal,
            requested_model=body.model,
            requested_tools=body.allowed_tools or None,
            requested_steps=body.max_steps,
            requested_tokens=body.max_tokens,
            requested_tool_calls=body.max_tool_calls,
            requested_cost_usd=body.max_cost_usd,
            requested_tool_cost_usd=body.max_tool_cost_usd,
            requested_wall_clock_s=body.max_wall_clock_s,
        )
    except PolicyViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "policy_violation",
                "message": str(exc),
                "field": exc.field,
                "requested": exc.requested,
                "limit": exc.limit,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_policy", "message": str(exc)},
        ) from exc

    # 2. Build and validate RunBudget
    try:
        budget = RunBudget(
            max_steps=effective_cfg.max_steps,
            max_tokens=effective_cfg.max_tokens,
            max_tool_calls=effective_cfg.max_tool_calls,
            max_cost_usd=effective_cfg.max_cost_usd,
            max_tool_cost_usd=effective_cfg.max_tool_cost_usd,
            max_wall_clock_s=effective_cfg.max_wall_clock_s,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_budget", "message": str(exc)},
        ) from exc

    # 3. Pre-allocate durable run_id
    run_id = str(uuid.uuid4())
    now_iso = datetime.now(UTC).isoformat()

    try:
        ctx = RunContext(
            run_id=run_id,
            workspace_id=ws_id_str,
            principal_id=principal.user_id,
            authz_policy_revision="rev1",
            model_policy=ModelPolicy(
                allowed_models=effective_cfg.allowed_models,
                default_model=effective_cfg.model,
            ),
            budget=budget,
            deadline=datetime.now(UTC)
            + timedelta(seconds=min(300.0, effective_cfg.max_wall_clock_s)),
            allowed_tools=effective_cfg.allowed_tools,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_context", "message": str(exc)},
        ) from exc

    # 4. Persist run record BEFORE 202 is returned (runs accepted without pre-allocated ID = 0)
    state = RunState(status=RunStatus.QUEUED)
    record = DurableRunRecord(
        run_id=run_id,
        workspace_id=ws_id_str,
        principal_id=principal.user_id,
        goal=body.goal,
        status=RunStatus.QUEUED,
        context=ctx,
        state=state,
        created_at=now_iso,
        updated_at=now_iso,
    )
    try:
        run_store = get_run_store()
        run_store.create_run(record)

        # 5. Enqueue to durable queue
        queue = get_queue()
        await queue.enqueue(run_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "storage_error", "message": f"Failed to persist queued run: {exc}"},
        ) from exc

    # 6. Return 202 Accepted
    return StartQueuedRunResponse(
        run_id=run_id,
        workspace_id=ws_id_str,
        status=RunStatus.QUEUED.value,
        created_at=now_iso,
    )


@router.get("/runs/{run_id}/status", response_model=StatusResponse)
async def get_run_status(
    workspace_id: uuid.UUID,
    run_id: str,
    principal: Principal = Depends(get_principal),
) -> StatusResponse:
    """Fetch status for an agent run, enforcing workspace isolation."""
    _require_workspace(principal, workspace_id)
    run_store = get_run_store()
    rec = run_store.get_run(run_id)
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "run not found"},
        )
    if rec.workspace_id != str(workspace_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "workspace_mismatch",
                "message": "run belongs to a different workspace",
            },
        )
    return StatusResponse(
        run_id=rec.run_id,
        workspace_id=rec.workspace_id,
        status=rec.status.value,
        error=rec.error,
        error_code=rec.error_code.value if rec.error_code else None,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
    )


@router.get("/runs/{run_id}/history", response_model=HistoryResponse)
async def get_run_history(
    workspace_id: uuid.UUID,
    run_id: str,
    principal: Principal = Depends(get_principal),
) -> HistoryResponse:
    """Fetch audit history of step events for a run."""
    _require_workspace(principal, workspace_id)
    run_store = get_run_store()
    rec = run_store.get_run(run_id)
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "run not found"},
        )
    if rec.workspace_id != str(workspace_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "workspace_mismatch",
                "message": "run belongs to a different workspace",
            },
        )
    step_store = get_step_store()
    events = step_store.list(run_id)
    return HistoryResponse(
        run_id=rec.run_id,
        workspace_id=rec.workspace_id,
        steps=[ev.model_dump() for ev in events],
        total_steps=len(events),
    )


@router.get("/runs", response_model=list[StatusResponse])
async def list_workspace_runs(
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
) -> list[StatusResponse]:
    """List all agent runs for a workspace."""
    _require_workspace(principal, workspace_id)
    run_store = get_run_store()
    records = run_store.list_runs(str(workspace_id))
    return [
        StatusResponse(
            run_id=r.run_id,
            workspace_id=r.workspace_id,
            status=r.status.value,
            error=r.error,
            error_code=r.error_code.value if r.error_code else None,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in records
    ]


@router.post("/runs/{run_id}/cancel", response_model=CancelResponse)
async def cancel_run(
    workspace_id: uuid.UUID,
    run_id: str,
    principal: Principal = Depends(get_principal),
) -> CancelResponse:
    """Cancel an agent run, checking workspace and enforcing monotonic state transitions."""
    _require_workspace(principal, workspace_id)
    run_store = get_run_store()
    rec = run_store.get_run(run_id)
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "run not found"},
        )
    if rec.workspace_id != str(workspace_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "workspace_mismatch",
                "message": "run belongs to a different workspace",
            },
        )

    # Server policy cancellation
    cancelled = rec.state.request_cancel()
    if cancelled:
        run_store.update_status(
            run_id,
            RunStatus.CANCELLED,
            error="run cancelled",
            error_code=AgentErrorCode.CANCELLED,
        )

    return CancelResponse(
        run_id=rec.run_id,
        workspace_id=rec.workspace_id,
        status=rec.state.status.value,
        cancelled=cancelled,
        error=rec.error,
        error_code=rec.error_code.value if rec.error_code else None,
    )


@router.post("/runs/{run_id}/approval", response_model=ApprovalResponse)
@router.post("/runs/{run_id}/approve", response_model=ApprovalResponse)
async def approve_or_reject_run(
    workspace_id: uuid.UUID,
    run_id: str,
    body: ApprovalRequest,
    principal: Principal = Depends(get_principal),
) -> ApprovalResponse:
    """Server-side approval/rejection for an agent run waiting on human approval."""
    _require_workspace(principal, workspace_id)
    run_store = get_run_store()
    rec = run_store.get_run(run_id)
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "run not found"},
        )
    if rec.workspace_id != str(workspace_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "workspace_mismatch",
                "message": "run belongs to a different workspace",
            },
        )

    if body.decision not in ("approved", "rejected"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_decision",
                "message": "decision must be 'approved' or 'rejected'",
            },
        )

    # Ensure run is in WAITING_APPROVAL
    if rec.state.status != RunStatus.WAITING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_state",
                "message": f"Run is in state '{rec.state.status.value}', not 'waiting_approval'",
            },
        )

    # Server policy check on the tool being approved
    ws_policy = get_workspace_policy(str(workspace_id))
    if body.tool_name is not None and body.tool_name not in ws_policy.allowed_tools:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "policy_violation",
                "message": (
                    f"Cannot approve action: tool {body.tool_name!r} is not allowed "
                    "by workspace policy"
                ),
            },
        )

    if body.decision == "approved":
        # Transition back to QUEUED and re-enqueue to resume execution
        rec.state.status = RunStatus.QUEUED
        run_store.update_status(run_id, RunStatus.QUEUED)
        queue = get_queue()
        await queue.enqueue(run_id)
        return ApprovalResponse(
            run_id=rec.run_id,
            workspace_id=rec.workspace_id,
            status=RunStatus.QUEUED.value,
            decision="approved",
        )
    else:
        rec.state.transition_to(
            RunStatus.CANCELLED,
            error_code=AgentErrorCode.CANCELLED,
            error_detail=f"action approval rejected: {body.feedback or 'rejected'}",
        )
        run_store.update_status(
            run_id,
            RunStatus.CANCELLED,
            error=f"action approval rejected: {body.feedback or 'rejected'}",
            error_code=AgentErrorCode.CANCELLED,
        )
        return ApprovalResponse(
            run_id=rec.run_id,
            workspace_id=rec.workspace_id,
            status=RunStatus.CANCELLED.value,
            decision="rejected",
            error="action approval rejected",
        )
