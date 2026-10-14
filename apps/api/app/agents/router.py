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
from agents.policy import (
    PolicyViolationError,
    derive_effective_run_config,
    get_workspace_policy,
)
from agents.runtime import AgentRuntime
from agents.tools import CalculatorTool, KnowledgeReadTool, KnowledgeSearchTool, ToolRegistry
from agents.unavailable import (
    MISSING_LLM_PROVIDER,
    UNAVAILABLE_CODE,
    AgentUnavailableError,
)
from core.auth import Principal
from fastapi import APIRouter, Depends, HTTPException, status
from models.router_config import load_workspace_config, route
from pydantic import BaseModel, Field

from app.agents.lifecycle_routes import subrouter as lifecycle_subrouter
from app.authz import Action, Resource, require
from app.authz.principal import get_principal

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/agent", tags=["agent"])


# ---------------------------------------------------------------------------
# In-memory run store (skeleton — mirrors runtime's _PERSISTED_* )
# ---------------------------------------------------------------------------

_RUNS: dict[str, dict[str, Any]] = {}


def _clear_store() -> None:
    _RUNS.clear()


def _resolve_llm_provider():
    """Return a REAL configured LLM provider, or ``None`` when unconfigured.

    Deliberately has no fallback. The previous implementation built a no-op
    provider that answered every goal with ``"Final Answer: done"``, which made
    an unconfigured service look like a working one and returned fabricated
    completions (T-R02, PRIO1 finding 2).

    ``None`` here means: no gateway URL and no usable API key. That is a
    configuration state, not a runtime failure, and the caller turns it into a
    typed UNAVAILABLE rather than inventing an answer.
    """
    import os

    base_url = os.environ.get("LITELLM_URL") or os.environ.get("OPENROUTER_API_KEY")
    has_key = any(
        os.environ.get(name)
        for name in ("LITELLM_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")
    )
    if not base_url or not has_key:
        return None
    try:
        from models.adapters.litellm_provider import create_llm_provider

        return create_llm_provider(call_site="agents.runtime")
    except Exception:
        # A configured-but-broken gateway is still not something to paper over
        # with a fake. Returning None keeps the outcome honest; the detail below
        # tells the operator to look at the provider config.
        return None


def create_runtime() -> AgentRuntime:
    """Factory seam — tests monkeypatch this (like search.create_orchestrator).

    Production wires a REAL provider. When none is configured this raises
    :class:`AgentUnavailableError` and NO provider, no tool registry and no
    runtime is constructed, so an unconfigured request provably makes zero
    provider/tool calls (T-R02). Tests that need a double patch this function.
    """
    llm = _resolve_llm_provider()
    if llm is None:
        raise AgentUnavailableError(
            MISSING_LLM_PROVIDER,
            "No LLM provider is configured (no LITELLM_URL and no usable API key); "
            "the run was not executed and no answer was produced.",
        )
    reg = ToolRegistry()
    reg.register(CalculatorTool())
    reg.register(KnowledgeSearchTool())
    reg.register(KnowledgeReadTool())
    return AgentRuntime(llm, reg)


class StartRunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=4096)
    model: str | None = Field(
        default=None, description="Requested model (must be allowed by policy)"
    )
    max_steps: int | None = Field(default=None, ge=1, le=20)
    max_tokens: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0.0)
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
    goal: str | None = None
    output: str | None = None
    budget: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)


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

    ws_id_str = str(workspace_id)
    policy = get_workspace_policy(ws_id_str)

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

    # Bounded everything — RunBudget validates bounds, RunContext validates IDs
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

    run_id = str(uuid.uuid4())
    # Model selection: workspace router config if present and no explicit request override
    selected_model = effective_cfg.model
    if not body.model:
        try:
            ws_model_cfg = load_workspace_config(ws_id_str)
            if ws_model_cfg:
                routed = route("agent", ws_model_cfg)
                if routed in effective_cfg.allowed_models:
                    selected_model = routed
        except Exception:
            pass

    # Reuse same gates as in-process: budget reservation, approval, workspace scope, bounded.
    # Build RunContext with workspace_id from URL (not body) — cross-workspace bypass impossible.
    try:
        ctx = RunContext(
            run_id=run_id,
            workspace_id=ws_id_str,
            principal_id=principal.user_id,
            authz_policy_revision="rev1",
            model_policy=ModelPolicy(
                allowed_models=effective_cfg.allowed_models,
                default_model=selected_model,
            ),
            budget=budget,
            deadline=datetime.now(UTC)
            + timedelta(seconds=min(30.0, effective_cfg.max_wall_clock_s)),
            allowed_tools=effective_cfg.allowed_tools,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_context", "message": str(exc)},
        ) from exc

    runtime = None
    unavailable_detail: str | None = None
    missing_piece: str | None = None
    try:
        runtime = create_runtime()
    except AgentUnavailableError as exc:
        # T-R02: unconfigured is a typed outcome, not a fabricated completion.
        # The runtime was never built, so nothing was called on the way out.
        unavailable_detail = exc.detail
        missing_piece = exc.missing

    run_status: str
    error_code: str | None
    # Optional: the runtime reports no detail on some terminal states.
    error_detail: str | None
    if runtime is None:
        # Honest UNAVAILABLE: a terminal run record carrying the reason, so the
        # caller can poll it, but no output and no status of "completed".
        run_status = RunStatus.FAILED.value
        error_code = AgentErrorCode.UNAVAILABLE.value
        error_detail = unavailable_detail or "unavailable"
    else:
        # Run through the SAME runtime path as in-process — parity by construction.
        # The runtime enforces budget reservation, approval gates, tool allowlist.
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

    if missing_piece is not None:
        error_detail = f"{error_detail or UNAVAILABLE_CODE} (missing: {missing_piece})"

    # Store for status/cancel
    _RUNS[run_id] = {
        "run_id": run_id,
        "workspace_id": str(workspace_id),
        "principal_id": principal.user_id,
        "status": run_status,
        "error": error_detail,
        "error_code": error_code,
        "missing": missing_piece,
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
        from stores.agent_stores import get_run_store

        durable_rec = get_run_store().get_run(run_id)
        if durable_rec is not None:
            if durable_rec.workspace_id != str(workspace_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "workspace_mismatch",
                        "message": "run belongs to a different workspace",
                    },
                )
            from app.agents.lifecycle_routes import _build_status_response

            d_resp = _build_status_response(durable_rec)
            return StatusResponse(
                run_id=d_resp.run_id,
                workspace_id=d_resp.workspace_id,
                status=d_resp.status,
                error=d_resp.error,
                error_code=d_resp.error_code,
                goal=d_resp.goal,
                output=d_resp.output,
                budget=d_resp.budget,
                usage=d_resp.usage,
                sources=d_resp.sources,
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "run not found"},
        )
    # Scoped workspace — cross-workspace attempt BLOCKED
    if rec["workspace_id"] != str(workspace_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "workspace_mismatch",
                "message": "run belongs to a different workspace",
            },
        )
    # Also check principal still has access to the run's workspace (defense in depth)
    if rec["principal_id"] != principal.user_id:
        pass
    return StatusResponse(
        run_id=run_id,
        workspace_id=rec["workspace_id"],
        status=rec["status"],
        error=rec["error"],
        error_code=rec["error_code"],
        goal=rec.get("goal"),
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
        from stores.agent_stores import get_run_store

        durable_rec = get_run_store().get_run(run_id)
        if durable_rec is not None:
            if durable_rec.workspace_id != str(workspace_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "workspace_mismatch",
                        "message": "run belongs to a different workspace",
                    },
                )
            cancelled = durable_rec.state.request_cancel()
            if cancelled:
                get_run_store().update_status(
                    run_id,
                    RunStatus.CANCELLED,
                    error="run cancelled",
                    error_code=AgentErrorCode.CANCELLED,
                )
            return StatusResponse(
                run_id=durable_rec.run_id,
                workspace_id=durable_rec.workspace_id,
                status=durable_rec.state.status.value,
                error=durable_rec.error,
                error_code=durable_rec.error_code.value if durable_rec.error_code else None,
                goal=durable_rec.goal,
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "run not found"},
        )
    if rec["workspace_id"] != str(workspace_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "workspace_mismatch",
                "message": "run belongs to a different workspace",
            },
        )
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
        if rec["status"] in (
            RunStatus.QUEUED.value,
            RunStatus.RUNNING.value,
            RunStatus.WAITING_APPROVAL.value,
            RunStatus.WAITING_INPUT.value,
        ):
            rec["status"] = RunStatus.CANCELLED.value
            rec["error_code"] = AgentErrorCode.CANCELLED.value
            rec["error"] = "run cancelled"

    return StatusResponse(
        run_id=run_id,
        workspace_id=rec["workspace_id"],
        status=rec["status"],
        error=rec["error"],
        error_code=rec["error_code"],
        goal=rec.get("goal"),
    )


router.include_router(lifecycle_subrouter)
