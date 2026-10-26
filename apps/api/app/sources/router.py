"""Connector source configuration HTTP routes (M6-001c backend)."""

from __future__ import annotations

import uuid
from typing import Literal

from connectors import ConnectorKind
from connectors.adapters.progress_store import SqlAlchemySyncProgressStore
from connectors.adapters.sync_queue import enqueue_sync_job
from connectors.sync_consumer import SourceIdentity, settings_fingerprint
from core.auth import Principal
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db import get_session
from app.authz import Action, ApiError, Resource, require
from app.authz.principal import get_principal
from app.sources.models import ConnectorConfigRecord
from app.sources.schemas import (
    ConnectorCreate,
    ConnectorPublic,
    ConnectorUpdate,
    SyncStatusResponse,
    SyncTriggerResponse,
)
from app.sources.service import SourceConfigService

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/sources", tags=["sources"])


def _authorize(workspace_id: uuid.UUID, principal: Principal) -> None:
    try:
        require(principal, Action.DOCUMENT_READ, Resource(workspace_id=str(workspace_id)))
    except ApiError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"error": exc.code, "message": exc.message}
        ) from exc


def _manage(workspace_id: uuid.UUID, principal: Principal) -> None:
    try:
        require(principal, Action.WORKSPACE_MANAGE, Resource(workspace_id=str(workspace_id)))
    except ApiError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"error": exc.code, "message": exc.message}
        ) from exc


def _progress_store(session: AsyncSession):
    from sqlalchemy.ext.asyncio import AsyncSession as _AS

    bind = session.bind

    def factory():
        return _AS(bind=bind, expire_on_commit=False)

    return SqlAlchemySyncProgressStore(session_factory=factory)


def _public(
    record: ConnectorConfigRecord,
    progress=None,
    last_error: str | None = None,
) -> ConnectorPublic:
    return ConnectorPublic(
        connector_id=record.connector_id,
        kind=record.kind,
        settings=record.settings or {},
        credential_ref=record.credential_ref,
        last_attempt_at=progress.last_attempt_at if progress else None,
        last_success_at=progress.last_success_at if progress else None,
        last_error=last_error,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _progress_error(progress, outcomes) -> str | None:
    source_error = getattr(progress, "last_error_code", None)
    if source_error:
        return source_error
    current_run = progress.last_run_id
    codes = [o.error_code for o in outcomes.values()
             if o.run_id == current_run and o.error_code]
    if codes:
        return codes[-1]
    return "INGESTION_ERROR" if progress.items_failed else None


def _progress_state(
    progress, error: str | None
) -> Literal["idle", "running", "succeeded", "failed"]:
    if progress is None or progress.last_attempt_at is None:
        return "idle"
    finished = getattr(progress, "last_finished_at", None)
    if finished is None or finished < progress.last_attempt_at:
        return "running"
    return "failed" if error else "succeeded"


@router.get("", response_model=list[ConnectorPublic])
async def list_sources(
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> list[ConnectorPublic]:
    _authorize(workspace_id, principal)
    service = SourceConfigService(session)
    records = await service.list_for_workspace(workspace_id)
    store = _progress_store(session)
    out = []
    for r in records:
        ident = SourceIdentity(
            workspace_id=str(workspace_id),
            connector_id=r.connector_id,
            kind=ConnectorKind(r.kind),
            settings_fingerprint=settings_fingerprint(r.settings or {}),
        )
        progress = await store.load(ident)
        last_error = None
        if progress is not None:
            outcomes = await store.outcomes(ident, progress.generation)
            last_error = _progress_error(progress, outcomes)
        out.append(_public(r, progress, last_error))
    return out


@router.post("", status_code=201, response_model=ConnectorPublic)
async def create_source(
    workspace_id: uuid.UUID,
    body: ConnectorCreate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> ConnectorPublic:
    _manage(workspace_id, principal)
    service = SourceConfigService(session)
    existing = await service.get(workspace_id, body.connector_id)
    if existing is not None:
        raise HTTPException(status_code=409, detail="connector already exists")
    try:
        record = await service.create(workspace_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _public(record)


@router.get("/{connector_id}", response_model=ConnectorPublic)
async def get_source(
    workspace_id: uuid.UUID,
    connector_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> ConnectorPublic:
    _authorize(workspace_id, principal)
    service = SourceConfigService(session)
    record = await service.get(workspace_id, connector_id)
    if record is None:
        raise HTTPException(status_code=404, detail="source not found")
    store = _progress_store(session)
    ident = SourceIdentity(
        workspace_id=str(workspace_id),
        connector_id=connector_id,
        kind=ConnectorKind(record.kind),
        settings_fingerprint=settings_fingerprint(record.settings or {}),
    )
    progress = await store.load(ident)
    last_error = None
    if progress is not None:
        outcomes = await store.outcomes(ident, progress.generation)
        last_error = _progress_error(progress, outcomes)
    return _public(record, progress, last_error)


@router.patch("/{connector_id}", response_model=ConnectorPublic)
async def update_source(
    workspace_id: uuid.UUID,
    connector_id: str,
    body: ConnectorUpdate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> ConnectorPublic:
    _manage(workspace_id, principal)
    service = SourceConfigService(session)
    try:
        record = await service.update(workspace_id, connector_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="source not found")
    return _public(record)


@router.delete("/{connector_id}", status_code=204)
async def delete_source(
    workspace_id: uuid.UUID,
    connector_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> None:
    _manage(workspace_id, principal)
    service = SourceConfigService(session)
    ok = await service.delete(workspace_id, connector_id)
    if not ok:
        raise HTTPException(status_code=404, detail="source not found")


@router.post("/{connector_id}/sync", response_model=SyncTriggerResponse)
async def trigger_sync(
    workspace_id: uuid.UUID,
    connector_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> SyncTriggerResponse:
    _manage(workspace_id, principal)
    service = SourceConfigService(session)
    record = await service.get(workspace_id, connector_id)
    if record is None:
        raise HTTPException(status_code=404, detail="source not found")
    try:
        job_id = await enqueue_sync_job(str(workspace_id), connector_id)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="sync queue unavailable"
        ) from exc
    return SyncTriggerResponse(
        job_id=job_id,
        workspace_id=str(workspace_id),
        connector_id=connector_id,
        acked=True,
    )


@router.get("/{connector_id}/status", response_model=SyncStatusResponse)
async def get_status(
    workspace_id: uuid.UUID,
    connector_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> SyncStatusResponse:
    _authorize(workspace_id, principal)
    service = SourceConfigService(session)
    record = await service.get(workspace_id, connector_id)
    if record is None:
        raise HTTPException(status_code=404, detail="source not found")
    store = _progress_store(session)
    ident = SourceIdentity(
        workspace_id=str(workspace_id),
        connector_id=connector_id,
        kind=ConnectorKind(record.kind),
        settings_fingerprint=settings_fingerprint(record.settings or {}),
    )
    progress = await store.load(ident)
    last_error = None
    if progress is not None:
        outcomes = await store.outcomes(ident, progress.generation)
        last_error = _progress_error(progress, outcomes)
    return SyncStatusResponse(
        connector_id=connector_id,
        kind=record.kind,
        last_attempt_at=progress.last_attempt_at if progress else None,
        last_success_at=progress.last_success_at if progress else None,
        last_error=last_error,
        last_run_id=progress.last_run_id if progress else None,
        last_finished_at=getattr(progress, "last_finished_at", None),
        state=_progress_state(progress, last_error),
        items_seen=progress.items_seen if progress else 0,
        items_created=progress.items_created if progress else 0,
        items_updated=progress.items_updated if progress else 0,
        items_unchanged=progress.items_unchanged if progress else 0,
        items_failed=progress.items_failed if progress else 0,
        items_deleted=progress.items_deleted if progress else 0,
        cursor=progress.cursor if progress else None,
    )
