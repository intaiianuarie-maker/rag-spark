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

import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

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
    get_evidence_store,
    get_queue,
    get_run_store,
    get_step_store,
)

from app.authz import Action, Resource, require
from app.authz.principal import get_principal

subrouter = APIRouter(tags=["agent-lifecycle"])
router = APIRouter(prefix="/v1/workspaces/{workspace_id}/agent", tags=["agent-lifecycle"])


# ---------------------------------------------------------------------------
# Durable agent store readiness (T-IMP-001, POLICE F4a/F4b)
# ---------------------------------------------------------------------------

#: Environments where a process-local (in-memory) agent store is tolerated.
#: Production and staging must use the shared durable store: an in-memory
#: queue accepts runs that no other process can consume (review 2026-10-19
#: finding 4: parent depth 1, independent worker depth 0).
LOCAL_ENVIRONMENTS = frozenset({"local", "test", "ci", ""})

#: Default shared root of the durable agent store. In the compose deployment
#: this is the named volume mounted into BOTH api and worker
#: (``agentdata:/app/.agent``); a path outside it is not shared storage.
DEFAULT_AGENT_STORE_SHARED_ROOT = "/app/.agent"

#: The source bind mounts the compose deployment puts inside the container
#: image tree. A shared root that covers one of them is NOT shared storage:
#: the agent volume is only present where nothing else is bound, so the store
#: file would live in a per-container filesystem and api and worker would
#: open different files (POLICE F4b, P2 shadowing finding).
CONTAINER_BIND_MOUNTS = ("/app/apps/api", "/app/apps/worker", "/app/packages")

#: The mount table the readiness consults. Overridable so the definitions can
#: be probed with a synthetic mountinfo file (no container required).
MOUNTINFO_PATH_ENV = "AGENT_STORE_MOUNTINFO"
DEFAULT_MOUNTINFO_PATH = "/proc/self/mountinfo"


class AgentStoreReadiness(NamedTuple):
    """Resolved readiness of the durable agent store.

    * ``admit``  - a queued run may be accepted (the store is durable and,
      in production/staging, shared between api and worker).
    * ``shared`` - the configured path lives inside the shared root.
    * ``reason`` - machine-readable reason for the verdict.
    * ``path``   - the configured durable path ("" when unset).
    * ``environment`` - the RESOLVED application environment.
    """

    admit: bool
    shared: bool
    reason: str
    path: str
    environment: str


def agent_store_readiness() -> AgentStoreReadiness:
    """Resolve the durable agent-store readiness (fail-closed).

    The environment comes from the RESOLVED application settings (process
    environment or ``.env``), never from a raw ``os.environ`` read that can
    disagree with the running application (POLICE F4a: a ``.env`` with
    ``APP_ENV=production`` used to log production while admitting runs into
    a process-local queue).

    A configured path is only *shared* when it lives inside the shared root
    (``AGENT_STORE_SHARED_ROOT``, default ``/app/.agent``). In production and
    staging an unshared path is refused: two containers with the same
    relative path have two different filesystems (POLICE F4b).
    """
    from core.config import get_settings

    settings = get_settings()
    environment = str(settings.app_env).strip().lower()
    raw_path = os.environ.get("AGENT_STORE_DB", "").strip()
    root = shared_root()
    local = environment in LOCAL_ENVIRONMENTS

    if not raw_path:
        return AgentStoreReadiness(
            admit=local,
            shared=False,
            reason="agent_store_not_configured",
            path="",
            environment=environment,
        )

    if root_is_shadowed(root):
        return AgentStoreReadiness(
            admit=local,
            shared=False,
            reason=(
                "agent_store_root_shadowed_dev_only"
                if local
                else "agent_store_root_shadowed"
            ),
            path=raw_path,
            environment=environment,
        )

    shared = _is_inside(raw_path, root) and root_is_mounted(root)
    if shared:
        return AgentStoreReadiness(
            admit=True,
            shared=True,
            reason="agent_store_shared",
            path=raw_path,
            environment=environment,
        )
    reason = (
        "agent_store_unshared_dev_only"
        if local
        else "agent_store_unshared"
    )
    if not root_is_mounted(root):
        reason = (
            "agent_store_root_not_mounted_dev_only"
            if local
            else "agent_store_root_not_mounted"
        )
    return AgentStoreReadiness(
        admit=local,
        shared=False,
        reason=reason,
        path=raw_path,
        environment=environment,
    )


def shared_root() -> str:
    """Return the shared root the durable agent store must live in."""
    return os.environ.get("AGENT_STORE_SHARED_ROOT", "").strip() or (
        DEFAULT_AGENT_STORE_SHARED_ROOT
    )


def shared_mount_target() -> str:
    """Return the path the shared agent volume is actually mounted at.

    The compose deployment mounts the ``agentdata`` named volume at the
    RESOLVED shared root in BOTH services, so the two are equal by
    construction. When they disagree (a hand-built deployment that overrides
    the root without moving the mount) the store is NOT shared storage and
    the readiness must not report ``shared=True`` (POLICE F4b).
    """
    return os.environ.get("AGENT_STORE_SHARED_MOUNT", "").strip()


def root_is_mounted(root: str) -> bool:
    """Return True when ``root`` is the mounted shared volume.

    Unknown (no declared mount target) counts as mounted: outside the
    compose deployment the api and worker processes share one host
    filesystem, and the deployment contract is the operator's.
    """
    mount_target = shared_mount_target()
    if not mount_target:
        return True
    try:
        return Path(mount_target).resolve() == Path(root).resolve()
    except OSError:  # pragma: no cover - unresolvable path
        return False


def _is_inside(path: str, root: str) -> bool:
    """Return True when ``path`` resolves inside ``root``."""
    try:
        resolved = Path(path).resolve()
        resolved_root = Path(root).resolve()
    except OSError:  # pragma: no cover - unresolvable path
        return False
    return resolved == resolved_root or resolved_root in resolved.parents

def shadow_mounts() -> list[str]:
    """Return the mount targets that can shadow the shared agent volume.

    The compose deployment bind-mounts the api/worker sources and the shared
    graph/workflow volumes into the container. When the resolved shared root
    covers one of those subtrees, the agent volume is only present where
    nothing else is bound: the store file would live in a per-container
    filesystem and the two processes would open different files
    (POLICE F4b, P2 shadowing finding).
    """
    raw = os.environ.get("AGENT_STORE_SHADOW_MOUNTS", "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


def _mount_points() -> list[str]:
    """Return the mount points of this process (best effort).

    The mount table can be redirected with ``AGENT_STORE_MOUNTINFO`` so the
    definitions can be probed with a synthetic mountinfo file (no container
    required).
    """
    path = (
        os.environ.get(MOUNTINFO_PATH_ENV, "").strip() or DEFAULT_MOUNTINFO_PATH
    )
    try:
        with open(path, encoding="utf-8") as handle:
            points = []
            for line in handle:
                fields = line.split(" ")
                # 5th field is the mount point (fields are escaped for spaces).
                if len(fields) > 4:
                    points.append(fields[4].replace("\\040", " "))
            return points
    except OSError:  # pragma: no cover - not Linux / unreadable
        return []


def root_is_shadowed(root: str) -> bool:
    """Return True when another mount covers part of ``root``.

    The INTENDED shared mount sits at the root's own mount point and is NOT
    shadowing: the agent volume is mounted exactly there. What shadows the
    root is a *competing* mount:

    1. a known bind mount (the compose source mounts, or the ones the
       deployment declares in ``AGENT_STORE_SHADOW_MOUNTS``) that lies inside
       the root or IS the root;
    2. a mount from the process mount table that lies strictly INSIDE the
       root (a nested mount covers the volume's subtree there).

    A mount that is an ANCESTOR of the root is the normal case (the root
    lives on that filesystem) and never shadows it, and the mount at the
    root's own path is the intended shared volume.
    """
    try:
        resolved_root = Path(root).resolve()
    except OSError:  # pragma: no cover - unresolvable path
        return True

    for candidate in list(CONTAINER_BIND_MOUNTS) + shadow_mounts():
        try:
            resolved = Path(candidate).resolve()
        except OSError:  # pragma: no cover - unresolvable path
            continue
        if resolved == resolved_root or resolved_root in resolved.parents:
            # A known competing mount covers the root or lives inside it.
            return True

    for candidate in _mount_points():
        try:
            resolved = Path(candidate).resolve()
        except OSError:  # pragma: no cover - unresolvable path
            continue
        if resolved_root in resolved.parents:
            # A mount lives INSIDE the root: the shared volume is shadowed
            # there, so the store would not be on the shared filesystem.
            return True
        # A mount AT the root is the intended shared volume; a mount ABOVE
        # the root is the filesystem the root lives on. Neither shadows it.
    return False


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
    goal: str | None = None
    output: str | None = None
    budget: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)


class ReadinessResponse(BaseModel):
    workspace_id: str
    ready: bool
    status: str
    missing: str | None = None
    detail: str | None = None


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
# Helpers
# ---------------------------------------------------------------------------


def _build_status_response(rec: DurableRunRecord) -> StatusResponse:
    output = rec.final_output
    if output is None:
        try:
            from agents.runtime import _PERSISTED_RUNS

            p_run = _PERSISTED_RUNS.get(rec.run_id)
            if p_run is not None:
                output = getattr(p_run, "final_output", None)
        except Exception:
            pass

    budget_dict: dict[str, Any] | None = None
    if rec.context is not None and rec.context.budget is not None:
        b = rec.context.budget
        budget_dict = {
            "max_steps": b.max_steps,
            "max_tokens": b.max_tokens,
            "max_tool_calls": b.max_tool_calls,
            "max_cost_usd": b.max_cost_usd,
            "max_tool_cost_usd": b.max_tool_cost_usd,
            "max_wall_clock_s": b.max_wall_clock_s,
        }

    usage_dict: dict[str, Any] | None = None
    try:
        step_store = get_step_store()
        steps = step_store.list(rec.run_id)
        if steps:
            total_prompt = sum(getattr(s.usage, "prompt_tokens", 0) for s in steps)
            total_completion = sum(getattr(s.usage, "completion_tokens", 0) for s in steps)
            total_tools = sum(getattr(s.usage, "tool_calls", 0) for s in steps)
            total_cost = sum(float(getattr(s.usage, "cost_usd", 0.0) or 0.0) for s in steps)
            usage_dict = {
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "tool_calls": total_tools,
                "cost_usd": total_cost,
            }
    except Exception:
        pass

    sources: list[dict[str, Any]] = []
    try:
        ev_store = get_evidence_store()
        ev_records: list[Any] = []
        if hasattr(ev_store, "_store") and isinstance(ev_store._store, dict):
            bucket = ev_store._store.get(rec.run_id, {})
            ev_records = list(bucket.values())
        elif hasattr(ev_store, "list"):
            ev_records = ev_store.list(rec.run_id)
        elif hasattr(ev_store, "path"):
            from agents.evidence import EvidenceRecord

            with sqlite3.connect(ev_store.path) as con:
                rows = con.execute(
                    "SELECT data FROM evidence WHERE run_id = ?", (rec.run_id,)
                ).fetchall()
                for row in rows:
                    ev_records.append(EvidenceRecord.model_validate(json.loads(row[0])))

        for ev in ev_records:
            for cit in getattr(ev, "citations", []):
                sources.append(
                    {
                        "document_id": getattr(cit, "document_id", None),
                        "chunk_id": getattr(cit, "chunk_id", ""),
                        "statement": getattr(ev, "statement", None) or getattr(ev, "claim", None),
                        "verdict": getattr(ev, "verdict", None),
                        "quote": getattr(cit, "quote", ""),
                    }
                )
    except Exception:
        pass

    return StatusResponse(
        run_id=rec.run_id,
        workspace_id=rec.workspace_id,
        status=rec.status.value,
        error=rec.error,
        error_code=rec.error_code.value if rec.error_code else None,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
        goal=rec.goal,
        output=output,
        budget=budget_dict,
        usage=usage_dict,
        sources=sources,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@subrouter.post(
    "/runs/queued",
    response_model=StartQueuedRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@subrouter.post(
    "/runs/async",
    response_model=StartQueuedRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
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

    # T-IMP-001 (POLICE F4a/F4b): fail-closed admission unless a shared
    # durable agent store is configured. The verdict comes from the RESOLVED
    # settings/startup boundary, and it runs BEFORE the run record is
    # persisted, so a refused run leaves nothing behind.
    readiness = agent_store_readiness()
    if not readiness.admit:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": readiness.reason,
                "message": (
                    "Queued agent runs require a shared durable agent store. "
                    "Set AGENT_STORE_DB to a path inside the shared root "
                    f"({shared_root()}) so api and worker use the same store."
                ),
                "environment": readiness.environment,
            },
        )

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


@subrouter.get("/readiness", response_model=ReadinessResponse)
@router.get("/readiness", response_model=ReadinessResponse)
async def get_agent_readiness(
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
) -> ReadinessResponse:
    """Check agent readiness for workspace (T-R02/T-R17).

    Returns honest unavailable state if no LLM provider is configured,
    never a fake ready-state.
    """
    _require_workspace(principal, workspace_id)
    import os

    base_url = os.environ.get("LITELLM_URL") or os.environ.get("OPENROUTER_API_KEY")
    has_key = any(
        os.environ.get(name)
        for name in ("LITELLM_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")
    )
    if bool(base_url and has_key):
        return ReadinessResponse(
            workspace_id=str(workspace_id),
            ready=True,
            status="ready",
            missing=None,
            detail="Agent runtime is ready",
        )
    return ReadinessResponse(
        workspace_id=str(workspace_id),
        ready=False,
        status="unavailable",
        missing="llm_provider",
        detail="No LLM provider is configured (missing: llm_provider)",
    )


@subrouter.get("/runs/{run_id}/status", response_model=StatusResponse)
@subrouter.get("/runs/{run_id}", response_model=StatusResponse)
@router.get("/runs/{run_id}/status", response_model=StatusResponse)
@router.get("/runs/{run_id}", response_model=StatusResponse)
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
    return _build_status_response(rec)


@subrouter.get("/runs/{run_id}/history", response_model=HistoryResponse)
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


@subrouter.get("/runs", response_model=list[StatusResponse])
@router.get("/runs", response_model=list[StatusResponse])
async def list_workspace_runs(
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
) -> list[StatusResponse]:
    """List all agent runs for a workspace."""
    _require_workspace(principal, workspace_id)
    run_store = get_run_store()
    records = run_store.list_runs(str(workspace_id))
    return [_build_status_response(r) for r in records]


@subrouter.post("/runs/{run_id}/cancel", response_model=CancelResponse)
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


@subrouter.post("/runs/{run_id}/approval", response_model=ApprovalResponse)
@subrouter.post("/runs/{run_id}/approve", response_model=ApprovalResponse)
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
        # T-A14: bind the approval to the real pending action BEFORE re-queueing.
        # Previously the route only flipped the state, so the runtime re-check
        # (_is_approved) never passed and a restored run looped WAITING forever.
        from agents.adapters.approval import (
            ApprovalActionMismatch,
            ApprovalAdapter,
            ApprovalAuthorityDenied,
            ApprovalBindingError,
            ApprovalPolicyStale,
        )

        current_revision = (
            rec.context.authz_policy_revision if rec.context is not None else None
        )
        try:
            binding = ApprovalAdapter.resolve_and_approve(
                run_id,
                approver_principal_id=principal.user_id,
                run_principal_id=rec.principal_id,
                current_policy_revision=current_revision,
                tool_name=body.tool_name,
            )
        except ApprovalAuthorityDenied as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": exc.code, "message": str(exc)},
            ) from exc
        except (
            ApprovalActionMismatch,
            ApprovalPolicyStale,
            ApprovalBindingError,
        ) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": exc.code, "message": str(exc)},
            ) from exc

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
            error=None if binding is not None else "approval_binding_unavailable",
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
