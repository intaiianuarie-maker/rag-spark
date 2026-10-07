"""Knowledge read endpoints (TASK-M2-003) — list/detail reads, no writes.

Frozen contract (boss 2026-10-07, `rag-fork-solution/TASKS.md` § TASK-M2-003):

- ``GET .../collections`` -> ``[{id,name,document_count,created_at}]``
- ``GET .../collections/{id}/documents`` ->
  ``[{id,title,filename,status,version_count,chunk_count,updated_at}]``
- ``GET .../documents/{doc}`` -> detail ``{…versions,jobs}``
- ``GET .../documents/{doc}/jobs`` ->
  ``[{id,state,attempts,error,created_at,updated_at}]``

(all four under ``/v1/workspaces/{ws}``; any shape change needs a boss ruling)

Design notes (see handoff for the two pre-implementation decisions):

- **Collections are implicit, one per workspace** (v1-minimal): there is no
  ``collections`` table, and the ticket is reads-only with "keep the schema
  small". ``collection.id`` is the workspace id, ``name`` is the workspace name,
  ``document_count`` is the live (non-tombstoned) document count. The nested
  documents route 404s when the given collection is not this workspace.
- **``status``/``state`` is the worker stage** (``queued``→``parsing``→
  ``chunking``→``embedding``→``indexing``→``ready``/``failed``), which is the
  precise worker state the M2-002a chips render; ``job.status`` only has
  ``queued``/``running``/``ready``/``failed`` and would lose that detail.
- Every route enforces workspace membership first (non-member -> 403).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from core.auth import Principal
from fastapi import APIRouter, Depends, HTTPException, status
from ingestion.dedup import latest_version
from ingestion.persistence import (
    ChunkRow,
    Document,
    DocumentVersion,
    IngestionJob,
)
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db import get_session
from app.auth.workspaces.models import Workspace
from app.authz import Action, ApiError, Resource, require
from app.authz.principal import get_principal

router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["knowledge"])


class CollectionSummary(BaseModel):
    id: str
    name: str
    document_count: int
    created_at: datetime


class DocumentVersionSummary(BaseModel):
    id: str
    version: int
    checksum: str
    created_at: datetime


class JobSummary(BaseModel):
    id: str
    state: str
    attempts: int
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class DocumentSummary(BaseModel):
    id: str
    title: str | None = None
    filename: str | None = None
    status: str | None = None
    version_count: int
    chunk_count: int
    updated_at: datetime


class DocumentDetail(DocumentSummary):
    """Summary + the detail fields the accepted M2-002a page reads.

    ``size_bytes``/``mime_type``/``current_version`` come from the frontend's
    own ``DocumentDetail`` type (`apps/web/src/app/knowledge/contracts.ts`);
    returning them is what makes the detail page render with zero frontend
    change. ``versions`` and ``jobs`` are the ticket-frozen additions.
    """

    created_at: datetime
    size_bytes: int | None = None
    mime_type: str | None = None
    current_version: DocumentVersionSummary | None = None
    versions: list[DocumentVersionSummary]
    jobs: list[JobSummary]


def _http_error(exc: ApiError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "message": exc.message},
    )


def _require_read(principal: Principal, workspace_id: uuid.UUID) -> None:
    try:
        require(
            principal, Action.DOCUMENT_READ, Resource(workspace_id=str(workspace_id))
        )
    except ApiError as exc:
        raise _http_error(exc) from exc


def _job_state(job: IngestionJob) -> str:
    """Worker state: the pipeline stage, falling back to status."""
    return job.stage or job.status


def _job_error(job: IngestionJob) -> str | None:
    return job.error_message or job.error_code


def _version_summary(version: DocumentVersion) -> DocumentVersionSummary:
    return DocumentVersionSummary(
        id=str(version.id),
        version=version.version_number,
        checksum=version.checksum,
        created_at=version.created_at,
    )


def _job_summary(job: IngestionJob) -> JobSummary:
    return JobSummary(
        id=str(job.id),
        state=_job_state(job),
        attempts=job.attempts,
        error=_job_error(job),
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


async def _latest_job_by_document(
    session: AsyncSession, document_ids: list[uuid.UUID]
) -> dict[uuid.UUID, IngestionJob]:
    """Most recent ingestion job per document (one query, no N+1)."""
    if not document_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(IngestionJob)
                .where(IngestionJob.document_id.in_(document_ids))
                .order_by(IngestionJob.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    latest: dict[uuid.UUID, IngestionJob] = {}
    for job in rows:
        latest.setdefault(job.document_id, job)
    return latest


async def _version_counts(
    session: AsyncSession, document_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not document_ids:
        return {}
    rows = await session.execute(
        select(DocumentVersion.document_id, func.count(DocumentVersion.id))
        .where(DocumentVersion.document_id.in_(document_ids))
        .group_by(DocumentVersion.document_id)
    )
    return {document_id: count for document_id, count in rows.all()}


async def _chunk_counts(
    session: AsyncSession, document_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Chunks per document, joined through its versions (superseded chunks are
    already deleted, so this equals the live chunk count)."""
    if not document_ids:
        return {}
    rows = await session.execute(
        select(DocumentVersion.document_id, func.count(ChunkRow.id))
        .join(ChunkRow, ChunkRow.document_version_id == DocumentVersion.id)
        .where(DocumentVersion.document_id.in_(document_ids))
        .group_by(DocumentVersion.document_id)
    )
    return {document_id: count for document_id, count in rows.all()}


def _summary(
    document: Document,
    *,
    latest_job: IngestionJob | None,
    version_count: int,
    chunk_count: int,
) -> DocumentSummary:
    return DocumentSummary(
        id=str(document.id),
        title=document.title,
        filename=document.source_filename,
        status=_job_state(latest_job) if latest_job is not None else None,
        version_count=version_count,
        chunk_count=chunk_count,
        updated_at=document.updated_at,
    )


@router.get(
    "/collections",
    response_model=list[CollectionSummary],
    summary="List the workspace's collections (implicit single collection)",
)
async def list_collections(
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> list[CollectionSummary]:
    _require_read(principal, workspace_id)

    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "workspace_not_found", "message": "Workspace not found."},
        )
    document_count = (
        await session.execute(
            select(func.count(Document.id)).where(
                Document.workspace_id == workspace_id,
                Document.deleted_at.is_(None),
            )
        )
    ).scalar_one()

    return [
        CollectionSummary(
            id=str(workspace_id),
            name=workspace.name,
            document_count=document_count,
            created_at=workspace.created_at,
        )
    ]


@router.get(
    "/collections/{collection_id}/documents",
    response_model=list[DocumentSummary],
    summary="List a collection's live documents with status/counts",
)
async def list_collection_documents(
    workspace_id: uuid.UUID,
    collection_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> list[DocumentSummary]:
    _require_read(principal, workspace_id)

    # Implicit collection: the only valid collection id is the workspace id.
    if collection_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "collection_not_found",
                "message": "Collection not found.",
            },
        )

    documents = list(
        (
            await session.execute(
                select(Document)
                .where(
                    Document.workspace_id == workspace_id,
                    Document.deleted_at.is_(None),
                )
                .order_by(Document.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    document_ids = [document.id for document in documents]
    latest_jobs = await _latest_job_by_document(session, document_ids)
    version_counts = await _version_counts(session, document_ids)
    chunk_counts = await _chunk_counts(session, document_ids)

    return [
        _summary(
            document,
            latest_job=latest_jobs.get(document.id),
            version_count=version_counts.get(document.id, 0),
            chunk_count=chunk_counts.get(document.id, 0),
        )
        for document in documents
    ]


async def _get_live_document(
    session: AsyncSession, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    document = await session.get(Document, document_id)
    if (
        document is None
        or document.workspace_id != workspace_id
        or document.deleted_at is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "document_not_found", "message": "Document not found."},
        )
    return document


@router.get(
    "/documents/{document_id}",
    response_model=DocumentDetail,
    summary="Document detail: metadata, versions, ingestion jobs",
)
async def get_document_detail(
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> DocumentDetail:
    _require_read(principal, workspace_id)
    document = await _get_live_document(session, workspace_id, document_id)

    versions = list(
        (
            await session.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document_id)
                .order_by(DocumentVersion.version_number.desc())
            )
        )
        .scalars()
        .all()
    )
    jobs = list(
        (
            await session.execute(
                select(IngestionJob)
                .where(
                    IngestionJob.workspace_id == workspace_id,
                    IngestionJob.document_id == document_id,
                )
                .order_by(IngestionJob.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    current_version = None
    if document.current_version_id is not None:
        current_version = await session.get(DocumentVersion, document.current_version_id)
    if current_version is None:
        current_version = await latest_version(session, document_id)

    chunk_count = 0
    if current_version is not None:
        chunk_count = (
            await session.execute(
                select(func.count(ChunkRow.id)).where(
                    ChunkRow.document_version_id == current_version.id
                )
            )
        ).scalar_one()

    latest_job = jobs[0] if jobs else None
    summary = _summary(
        document,
        latest_job=latest_job,
        version_count=len(versions),
        chunk_count=chunk_count,
    )
    return DocumentDetail(
        **summary.model_dump(),
        created_at=document.created_at,
        size_bytes=current_version.size_bytes if current_version else None,
        mime_type=document.mime_type,
        current_version=_version_summary(current_version) if current_version else None,
        versions=[_version_summary(version) for version in versions],
        jobs=[_job_summary(job) for job in jobs],
    )


@router.get(
    "/documents/{document_id}/jobs",
    response_model=list[JobSummary],
    summary="List a document's ingestion jobs (newest first)",
)
async def list_document_jobs(
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> list[JobSummary]:
    _require_read(principal, workspace_id)
    await _get_live_document(session, workspace_id, document_id)

    jobs = list(
        (
            await session.execute(
                select(IngestionJob)
                .where(
                    IngestionJob.workspace_id == workspace_id,
                    IngestionJob.document_id == document_id,
                )
                .order_by(IngestionJob.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_job_summary(job) for job in jobs]
