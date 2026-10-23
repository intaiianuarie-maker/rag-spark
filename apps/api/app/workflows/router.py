"""Authorized and workspace-scoped V4 Visual Workflows API handlers (T-R19).

Implements 8 core operations:
1. draft   - retrieve latest draft, specific revision, or revision history
2. import  - import and translate Langflow or native definitions into validated drafts
3. validate - validate workflow DAGs against capability manifest and emit annotations
4. save    - store revision 1 or append revision to history with append-only enforcement
5. approve - approve specific revision for execution after validation verification
6. run     - compile revision and execute execution plan
7. status  - retrieve workflow execution status and terminal timeline model
8. cancel  - cancel in-flight or running execution monotonically

Every single path enforces workspace authorization scoping (Action.WORKSPACE_READ /
Action.WORKSPACE_MANAGE). Cross-workspace access is rejected with HTTP 403.
Append-only and store invariants are never swallowed, returning HTTP 409 / 413 / 422.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

from core.auth import Principal
from fastapi import APIRouter, Depends, HTTPException, Query, status
from workflow_engine.adapters.langflow_adapter import LangflowAdapter, LangflowImportError
from workflow_engine.adapters.v3_adapter import resolve_node_executor
from workflow_engine.checkpoint import SqliteCheckpointStore
from workflow_engine.compiler import WorkflowCompiler
from workflow_engine.draft_store import (
    DraftConflictError,
    DraftIdentityError,
    DraftNotFoundError,
    DraftRevision,
    DraftWorkspaceError,
)
from workflow_engine.executor import WorkflowExecutor
from workflow_engine.interfaces import (
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
)
from workflow_engine.manifest import PRODUCTION_CAPABILITY_MANIFEST
from workflow_engine.production_executor import ProductionNodeExecutor
from workflow_engine.routes.schemas import (
    DraftErrorCode,
    DraftHistoryResponse,
    DraftMutationResponse,
    DraftReviseRequest,
    DraftRevisionDetail,
    DraftRevisionMeta,
    DraftSaveRequest,
    assert_path_scope_matches,
)
from workflow_engine.store_discipline import StoreBoundsError, StoreSecretError
from workflow_engine.validator import WorkflowValidator
from workflow_engine.view_models import build_annotations, build_timeline

from app.authz import Action, AuthorizationError, Resource, require
from app.authz.principal import get_principal
from app.workflows.schemas import (
    DraftListResponse,
    WorkflowApproveRequest,
    WorkflowApproveResponse,
    WorkflowImportRequest,
    WorkflowRunRequest,
    WorkflowRunResponse,
    WorkflowValidateRequest,
    WorkflowValidateResponse,
)
from app.workflows.store import (
    cancel_run,
    clear_dispatch,
    get_draft_store,
    get_run,
    persist_run,
    record_approval,
    register_dispatch,
    register_run,
    save_run,
)

router = APIRouter(tags=["v4-workflows"])


def _handle_store_error(exc: Exception) -> HTTPException:
    """Map store and domain errors honestly to HTTP responses without swallowing."""
    if isinstance(exc, DraftConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": DraftErrorCode.DRAFT_CONFLICT.value, "message": str(exc)},
        )
    if isinstance(exc, DraftNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": DraftErrorCode.DRAFT_NOT_FOUND.value, "message": str(exc)},
        )
    if isinstance(exc, DraftIdentityError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": DraftErrorCode.DRAFT_IDENTITY_MISMATCH.value, "message": str(exc)},
        )
    if isinstance(exc, DraftWorkspaceError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": DraftErrorCode.DRAFT_WORKSPACE_MISMATCH.value, "message": str(exc)},
        )
    if isinstance(exc, StoreBoundsError):
        return HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={"error": DraftErrorCode.DRAFT_TOO_LARGE.value, "message": str(exc)},
        )
    if isinstance(exc, StoreSecretError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": DraftErrorCode.DRAFT_SECRET_REFUSED.value, "message": str(exc)},
        )
    if isinstance(exc, ValueError):
        err_msg = str(exc)
        code = DraftErrorCode.DRAFT_VALIDATION_FAILED.value
        if DraftErrorCode.DRAFT_WORKSPACE_MISMATCH.value in err_msg:
            code = DraftErrorCode.DRAFT_WORKSPACE_MISMATCH.value
        elif DraftErrorCode.DRAFT_IDENTITY_MISMATCH.value in err_msg:
            code = DraftErrorCode.DRAFT_IDENTITY_MISMATCH.value
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": code, "message": err_msg},
        )
    if isinstance(exc, AuthorizationError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": exc.code, "message": exc.message},
        )
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "operation_failed", "message": str(exc)},
    )


def _draft_revision_detail(draft: DraftRevision) -> DraftRevisionDetail:
    return DraftRevisionDetail(
        workflow_id=draft.workflow_id,
        workspace_id=draft.workspace_id,
        revision=draft.number,
        content_hash=draft.content_hash,
        summary=draft.summary,
        parent_revision=draft.parent_revision,
        parent_hash=draft.parent_hash,
        created_at=draft.revision.created_at,
        spec=draft.spec,
    )


def _draft_mutation_response(draft: DraftRevision, idempotent: bool) -> DraftMutationResponse:
    return DraftMutationResponse(
        workflow_id=draft.workflow_id,
        workspace_id=draft.workspace_id,
        revision=draft.number,
        content_hash=draft.content_hash,
        summary=draft.summary,
        parent_revision=draft.parent_revision,
        parent_hash=draft.parent_hash,
        created_at=draft.revision.created_at,
        spec=draft.spec,
        idempotent=idempotent,
    )


# --------------------------------------------------------------------------- #
# 1. DRAFT Handlers (List, Latest, Revision, History)
# --------------------------------------------------------------------------- #


@router.get(
    "/v1/workspaces/{workspace_id}/drafts",
    response_model=DraftListResponse,
    summary="List all workflow drafts in a workspace",
)
async def list_drafts(
    workspace_id: str,
    principal: Principal = Depends(get_principal),
) -> DraftListResponse:
    try:
        require(principal, Action.WORKSPACE_READ, Resource(workspace_id=workspace_id))
    except AuthorizationError as exc:
        raise _handle_store_error(exc) from exc

    store = get_draft_store()
    workflow_ids = await store.workflow_ids(workspace_id)
    items: list[DraftRevisionMeta] = []
    for wid in workflow_ids:
        latest = await store.latest(workspace_id, wid)
        if latest is not None:
            items.append(
                DraftRevisionMeta(
                    workflow_id=latest.workflow_id,
                    workspace_id=latest.workspace_id,
                    revision=latest.number,
                    content_hash=latest.content_hash,
                    summary=latest.summary,
                    parent_revision=latest.parent_revision,
                    parent_hash=latest.parent_hash,
                    created_at=latest.revision.created_at,
                )
            )

    return DraftListResponse(
        workspace_id=workspace_id,
        count=len(items),
        drafts=items,
    )


@router.get(
    "/v1/workspaces/{workspace_id}/drafts/{workflow_id}",
    response_model=DraftRevisionDetail,
    summary="Get the head revision of a draft workflow",
)
async def get_draft_latest(
    workspace_id: str,
    workflow_id: str,
    principal: Principal = Depends(get_principal),
) -> DraftRevisionDetail:
    try:
        require(principal, Action.WORKSPACE_READ, Resource(workspace_id=workspace_id))
    except AuthorizationError as exc:
        raise _handle_store_error(exc) from exc

    store = get_draft_store()
    draft = await store.latest(workspace_id, workflow_id)
    if draft is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": DraftErrorCode.DRAFT_NOT_FOUND.value,
                "message": f"Draft workflow {workflow_id!r} not found in workspace",
            },
        )
    return _draft_revision_detail(draft)


@router.get(
    "/v1/workspaces/{workspace_id}/drafts/{workflow_id}/revisions/{revision}",
    response_model=DraftRevisionDetail,
    summary="Get a specific revision of a draft workflow",
)
async def get_draft_revision(
    workspace_id: str,
    workflow_id: str,
    revision: int,
    principal: Principal = Depends(get_principal),
) -> DraftRevisionDetail:
    try:
        require(principal, Action.WORKSPACE_READ, Resource(workspace_id=workspace_id))
    except AuthorizationError as exc:
        raise _handle_store_error(exc) from exc

    store = get_draft_store()
    try:
        draft = await store.get(workspace_id, workflow_id, revision)
    except Exception as exc:
        raise _handle_store_error(exc) from exc
    return _draft_revision_detail(draft)


@router.get(
    "/v1/workspaces/{workspace_id}/drafts/{workflow_id}/revisions",
    response_model=DraftHistoryResponse,
    summary="List revision history of a draft workflow",
)
async def list_draft_history(
    workspace_id: str,
    workflow_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
) -> DraftHistoryResponse:
    try:
        require(principal, Action.WORKSPACE_READ, Resource(workspace_id=workspace_id))
    except AuthorizationError as exc:
        raise _handle_store_error(exc) from exc

    store = get_draft_store()
    try:
        history = await store.history(workspace_id, workflow_id)
    except Exception as exc:
        raise _handle_store_error(exc) from exc

    if not history:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": DraftErrorCode.DRAFT_NOT_FOUND.value,
                "message": f"Workflow {workflow_id!r} does not exist in workspace",
            },
        )

    meta_list = [
        DraftRevisionMeta(
            workflow_id=rev.workflow_id,
            workspace_id=rev.workspace_id,
            revision=rev.number,
            content_hash=rev.content_hash,
            summary=rev.summary,
            parent_revision=rev.parent_revision,
            parent_hash=rev.parent_hash,
            created_at=rev.revision.created_at,
        )
        for rev in history[:limit]
    ]

    return DraftHistoryResponse(
        workflow_id=workflow_id,
        workspace_id=workspace_id,
        count=len(meta_list),
        revisions=meta_list,
    )


# --------------------------------------------------------------------------- #
# 2. IMPORT Handler
# --------------------------------------------------------------------------- #


@router.post(
    "/v1/workspaces/{workspace_id}/drafts/import",
    response_model=DraftMutationResponse,
    summary="Import workflow from Langflow export or native definition",
)
@router.post(
    "/v1/workspaces/{workspace_id}/workflows/import",
    response_model=DraftMutationResponse,
    include_in_schema=False,
)
async def import_workflow(
    workspace_id: str,
    request: WorkflowImportRequest,
    principal: Principal = Depends(get_principal),
) -> DraftMutationResponse:
    try:
        require(principal, Action.WORKSPACE_MANAGE, Resource(workspace_id=workspace_id))
    except AuthorizationError as exc:
        raise _handle_store_error(exc) from exc

    flow_data = request.flow_data
    flow_id = (
        flow_data.get("id")
        if isinstance(flow_data, dict) and isinstance(flow_data.get("id"), str)
        else None
    )
    wid = request.workflow_id or flow_id or f"wf-{uuid.uuid4().hex[:8]}"

    try:
        is_langflow = False
        if "data" in flow_data and isinstance(flow_data["data"], dict):
            is_langflow = "nodes" in flow_data["data"]
        elif "nodes" in flow_data and isinstance(flow_data["nodes"], list):
            for n in flow_data["nodes"]:
                if (
                    isinstance(n, dict)
                    and "data" in n
                    and isinstance(n["data"], dict)
                    and "type" in n["data"]
                ):
                    is_langflow = True
                    break

        if is_langflow:
            definition = LangflowAdapter.import_definition(
                flow_data,
                workflow_id=wid,
                workspace_id=workspace_id,
                name=request.name or "Imported Flow",
                description=request.description or "",
            )
        else:
            payload = dict(flow_data)
            payload["id"] = wid
            payload["workspace_id"] = workspace_id
            if request.name:
                payload["name"] = request.name
            if request.description is not None:
                payload["description"] = request.description
            definition = WorkflowDefinition.model_validate(payload)
    except LangflowImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "import_failed",
                "message": str(exc),
                "rejections": getattr(exc, "rejections", []),
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "import_failed", "message": f"Failed to parse workflow: {exc}"},
        ) from exc

    store = get_draft_store()
    try:
        existing = await store.latest(workspace_id, definition.id)
        if existing is not None:
            rev = await store.revise(workspace_id, definition.id, definition)
            idempotent = rev.number == existing.number
        else:
            rev = await store.save_draft(definition)
            idempotent = False
    except Exception as exc:
        raise _handle_store_error(exc) from exc

    return _draft_mutation_response(rev, idempotent=idempotent)


# --------------------------------------------------------------------------- #
# 3. VALIDATE Handler
# --------------------------------------------------------------------------- #


@router.post(
    "/v1/workspaces/{workspace_id}/drafts/validate",
    response_model=WorkflowValidateResponse,
    summary="Validate a workflow definition or stored draft against capabilities",
)
@router.post(
    "/v1/workspaces/{workspace_id}/workflows/validate",
    response_model=WorkflowValidateResponse,
    include_in_schema=False,
)
async def validate_workflow(
    workspace_id: str,
    request: WorkflowValidateRequest,
    principal: Principal = Depends(get_principal),
) -> WorkflowValidateResponse:
    try:
        require(principal, Action.WORKSPACE_READ, Resource(workspace_id=workspace_id))
    except AuthorizationError as exc:
        raise _handle_store_error(exc) from exc

    definition: WorkflowDefinition | None = request.definition
    store = get_draft_store()

    if definition is None:
        if not request.workflow_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "validation_failed",
                    "message": "Either definition or workflow_id must be provided",
                },
            )
        loaded_draft: DraftRevision | None = None
        try:
            if request.revision:
                loaded_draft = await store.get(workspace_id, request.workflow_id, request.revision)
            else:
                loaded_draft = await store.latest(workspace_id, request.workflow_id)
            if loaded_draft is None:
                raise DraftNotFoundError(f"Workflow {request.workflow_id!r} not found")
            definition = loaded_draft.spec
        except Exception as exc:
            raise _handle_store_error(exc) from exc

    if definition.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": DraftErrorCode.DRAFT_WORKSPACE_MISMATCH.value,
                "message": (
                    f"Workflow workspace {definition.workspace_id!r} != path {workspace_id!r}"
                ),
            },
        )

    manifest = PRODUCTION_CAPABILITY_MANIFEST
    result = WorkflowValidator.validate(definition, manifest)
    annotations = build_annotations(result)

    return WorkflowValidateResponse(
        valid=result.valid,
        errors=result.errors,
        annotations=annotations,
    )


# --------------------------------------------------------------------------- #
# 4. SAVE Handlers (Initial Save + Revise)
# --------------------------------------------------------------------------- #


@router.post(
    "/v1/workspaces/{workspace_id}/drafts",
    response_model=DraftMutationResponse,
    summary="Save a new workflow definition as revision 1",
)
async def save_draft(
    workspace_id: str,
    body: DraftSaveRequest,
    principal: Principal = Depends(get_principal),
) -> DraftMutationResponse:
    try:
        require(principal, Action.WORKSPACE_MANAGE, Resource(workspace_id=workspace_id))
    except AuthorizationError as exc:
        raise _handle_store_error(exc) from exc

    try:
        assert_path_scope_matches(body, workspace_id=workspace_id)
        store = get_draft_store()
        draft = await store.save_draft(body)
    except Exception as exc:
        raise _handle_store_error(exc) from exc

    return _draft_mutation_response(draft, idempotent=False)


@router.post(
    "/v1/workspaces/{workspace_id}/drafts/{workflow_id}/revisions",
    response_model=DraftMutationResponse,
    summary="Append a new revision to an existing draft workflow",
)
async def revise_draft(
    workspace_id: str,
    workflow_id: str,
    body: DraftReviseRequest,
    principal: Principal = Depends(get_principal),
) -> DraftMutationResponse:
    try:
        require(principal, Action.WORKSPACE_MANAGE, Resource(workspace_id=workspace_id))
    except AuthorizationError as exc:
        raise _handle_store_error(exc) from exc

    try:
        assert_path_scope_matches(body, workspace_id=workspace_id, workflow_id=workflow_id)
        store = get_draft_store()
        head_before = await store.latest(workspace_id, workflow_id)
        draft = await store.revise(workspace_id, workflow_id, body)
        idempotent = head_before is not None and draft.number == head_before.number
    except Exception as exc:
        raise _handle_store_error(exc) from exc

    return _draft_mutation_response(draft, idempotent=idempotent)


# --------------------------------------------------------------------------- #
# 5. APPROVE Handler
# --------------------------------------------------------------------------- #


@router.post(
    "/v1/workspaces/{workspace_id}/drafts/{workflow_id}/approve",
    response_model=WorkflowApproveResponse,
    summary="Approve a workflow revision for production execution",
)
@router.post(
    "/v1/workspaces/{workspace_id}/workflows/{workflow_id}/approve",
    response_model=WorkflowApproveResponse,
    include_in_schema=False,
)
async def approve_workflow(
    workspace_id: str,
    workflow_id: str,
    request: WorkflowApproveRequest,
    principal: Principal = Depends(get_principal),
) -> WorkflowApproveResponse:
    try:
        require(principal, Action.WORKSPACE_MANAGE, Resource(workspace_id=workspace_id))
    except AuthorizationError as exc:
        raise _handle_store_error(exc) from exc

    store = get_draft_store()
    loaded_draft: DraftRevision | None = None
    try:
        if request.revision is not None:
            loaded_draft = await store.get(workspace_id, workflow_id, request.revision)
        else:
            loaded_draft = await store.latest(workspace_id, workflow_id)
        if loaded_draft is None:
            raise DraftNotFoundError(f"Workflow {workflow_id!r} not found in workspace")
        draft: DraftRevision = loaded_draft
    except Exception as exc:
        raise _handle_store_error(exc) from exc

    # Validate definition before approval
    manifest = PRODUCTION_CAPABILITY_MANIFEST
    val_result = WorkflowValidator.validate(draft.spec, manifest)
    if not val_result.valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "approval_failed_validation",
                "message": (
                    f"Cannot approve invalid workflow revision: {val_result.error_summary}"
                ),
            },
        )

    approval = WorkflowApproveResponse(
        approved=True,
        workflow_id=draft.workflow_id,
        workspace_id=draft.workspace_id,
        revision=draft.number,
        content_hash=draft.content_hash,
        approved_by=principal.user_id,
        approved_at=datetime.now(UTC).isoformat(),
        comment=request.comment,
    )
    record_approval(approval)
    return approval


# --------------------------------------------------------------------------- #
# 6. RUN Handler
# --------------------------------------------------------------------------- #


@router.post(
    "/v1/workspaces/{workspace_id}/workflows/{workflow_id}/run",
    response_model=WorkflowRunResponse,
    summary="Execute a validated or approved workflow revision",
)
@router.post(
    "/v1/workspaces/{workspace_id}/drafts/{workflow_id}/run",
    response_model=WorkflowRunResponse,
    include_in_schema=False,
)
async def run_workflow(
    workspace_id: str,
    workflow_id: str,
    request: WorkflowRunRequest,
    principal: Principal = Depends(get_principal),
) -> WorkflowRunResponse:
    try:
        require(principal, Action.WORKSPACE_MANAGE, Resource(workspace_id=workspace_id))
    except AuthorizationError as exc:
        raise _handle_store_error(exc) from exc

    try:
        checkpoint_db_path()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "checkpoint_unavailable", "message": str(exc)},
        ) from exc

    store = get_draft_store()
    loaded_run_draft: DraftRevision | None = None
    try:
        if request.revision is not None:
            loaded_run_draft = await store.get(workspace_id, workflow_id, request.revision)
        else:
            loaded_run_draft = await store.latest(workspace_id, workflow_id)
        if loaded_run_draft is None:
            raise DraftNotFoundError(f"Workflow {workflow_id!r} not found in workspace")
        draft = loaded_run_draft
    except Exception as exc:
        raise _handle_store_error(exc) from exc

    run_id = f"wfrun-{uuid.uuid4().hex[:8]}"

    try:
        try:
            plan = WorkflowCompiler.compile(draft.revision)
        except Exception as exc:
            # Invalid definition: refuse as HTTP 422 with a safe, no-raw-text message.
            safe = "invalid workflow definition"
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error": "invalid_definition", "message": safe},
            ) from exc
        try:
            node_executor = resolve_node_executor(default_factory=ProductionNodeExecutor)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "v3_adapter_unconfigured", "message": str(exc)},
            ) from exc
        executor = WorkflowExecutor(node_executor=node_executor)
        try:
            executor.configure_checkpoint_store(SqliteCheckpointStore(checkpoint_db_path()))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "checkpoint_unavailable", "message": str(exc)},
            ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # Compile/executor wiring failure: coded error, raw text stays server-side.
        safe_code = "runtime_failure"
        run = WorkflowRun(
            id=run_id,
            workspace_id=workspace_id,
            workflow_id=workflow_id,
            status=WorkflowRunStatus.FAILED,
            error=f"{safe_code}: {type(exc).__name__}",
            created_at=datetime.now(UTC).isoformat(),
            finished_at=datetime.now(UTC).isoformat(),
        )
        save_run(run, None)
        timeline = build_timeline(run)
        return WorkflowRunResponse(run=run, timeline=timeline)

    # T-A16: register the run row AND its cancel handle BEFORE dispatch, so the
    # run id exists and status/cancel can control the in-flight run. Previously
    # the route awaited execute() first, leaving no run id and no cancel surface.
    initial_run = WorkflowRun(
        id=run_id,
        workspace_id=workspace_id,
        workflow_id=workflow_id,
        status=WorkflowRunStatus.RUNNING,
        initial_input=request.inputs,
        created_at=datetime.now(UTC).isoformat(),
    )
    register_run(initial_run, executor)

    async def _supervise_run() -> None:
        """Execute and persist the final state; never raise into the loop."""
        try:
            final_run = await executor.execute(
                plan,
                draft.revision,
                initial_input=request.inputs,
                run_id=run_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Node/runtime failure: coded error, raw text stays server-side.
            safe_code = "runtime_failure"
            final_run = WorkflowRun(
                id=run_id,
                workspace_id=workspace_id,
                workflow_id=workflow_id,
                status=WorkflowRunStatus.FAILED,
                initial_input=request.inputs,
                error=f"{safe_code}: {type(exc).__name__}",
                created_at=initial_run.created_at,
                finished_at=datetime.now(UTC).isoformat(),
            )
        # Monotonic: a run already CANCELLED by the API keeps CANCELLED.
        stored = get_run(workspace_id, run_id)
        if stored is not None and stored.status == WorkflowRunStatus.CANCELLED:
            final_run = final_run.model_copy(
                update={
                    "status": WorkflowRunStatus.CANCELLED,
                    "error": stored.error or final_run.error,
                    "finished_at": stored.finished_at or final_run.finished_at,
                }
            )
        persist_run(final_run, executor)

    task = asyncio.create_task(_supervise_run())
    register_dispatch(run_id, task)
    task.add_done_callback(lambda _t: clear_dispatch(run_id))

    timeline = build_timeline(initial_run)
    return WorkflowRunResponse(run=initial_run, timeline=timeline)


# --------------------------------------------------------------------------- #
# 7. STATUS Handler
# --------------------------------------------------------------------------- #



def checkpoint_db_path() -> Path:
    """Resolve the durable checkpoint DB path (fail-closed)."""
    import os

    raw = os.environ.get("WORKFLOW_CHECKPOINT_DB")
    if raw is None or not raw.strip():
        raise ValueError("WORKFLOW_CHECKPOINT_DB is required")
    return Path(raw.strip())



@router.get(
    "/v1/workspaces/{workspace_id}/workflows/{workflow_id}/runs/{run_id}",
    response_model=WorkflowRunResponse,
    summary="Get status and execution timeline for a workflow run",
)
@router.get(
    "/v1/workspaces/{workspace_id}/runs/{run_id}",
    response_model=WorkflowRunResponse,
    include_in_schema=False,
)
async def get_workflow_run_status(
    workspace_id: str,
    run_id: str,
    workflow_id: str | None = None,
    principal: Principal = Depends(get_principal),
) -> WorkflowRunResponse:
    try:
        require(principal, Action.WORKSPACE_READ, Resource(workspace_id=workspace_id))
    except AuthorizationError as exc:
        raise _handle_store_error(exc) from exc

    run = get_run(workspace_id, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "run_not_found", "message": f"Run {run_id!r} not found in workspace"},
        )

    if workflow_id and run.workflow_id != workflow_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "run_not_found",
                "message": (
                    f"Run {run_id!r} does not belong to workflow {workflow_id!r}"
                ),
            },
        )

    timeline = build_timeline(run)
    return WorkflowRunResponse(run=run, timeline=timeline)


# --------------------------------------------------------------------------- #
# 8. CANCEL Handler
# --------------------------------------------------------------------------- #


@router.post(
    "/v1/workspaces/{workspace_id}/workflows/{workflow_id}/runs/{run_id}/cancel",
    response_model=WorkflowRunResponse,
    summary="Cancel an active workflow run monotonically",
)
@router.post(
    "/v1/workspaces/{workspace_id}/runs/{run_id}/cancel",
    response_model=WorkflowRunResponse,
    include_in_schema=False,
)
async def cancel_workflow_run(
    workspace_id: str,
    run_id: str,
    workflow_id: str | None = None,
    principal: Principal = Depends(get_principal),
) -> WorkflowRunResponse:
    try:
        require(principal, Action.WORKSPACE_MANAGE, Resource(workspace_id=workspace_id))
    except AuthorizationError as exc:
        raise _handle_store_error(exc) from exc

    run = get_run(workspace_id, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "run_not_found", "message": f"Run {run_id!r} not found in workspace"},
        )

    if workflow_id and run.workflow_id != workflow_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "run_not_found",
                "message": (
                    f"Run {run_id!r} does not belong to workflow {workflow_id!r}"
                ),
            },
        )

    try:
        updated_run = cancel_run(workspace_id, run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "no_live_executor", "message": str(exc)},
        ) from exc
    if updated_run is None:
        updated_run = run

    timeline = build_timeline(updated_run)
    return WorkflowRunResponse(run=updated_run, timeline=timeline)
