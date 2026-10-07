"""Document upload + ingestion-job status (M2-001c).

The API never parses: it stores the original bytes (interim, in
``document_versions.content`` until PD-005 moves them to MinIO), creates a
``queued`` ingestion job, enqueues the job id on Redis, and returns in one
round-trip. Parsing/chunking happen in the worker.
"""

from __future__ import annotations

import codecs
import hashlib
import uuid
from datetime import datetime

from core.auth import Principal
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from ingestion.normalize import MAX_DOCUMENT_BYTES
from ingestion.persistence import (
    Document,
    DocumentVersion,
    IngestionJob,
    JobStage,
    JobStatus,
)
from ingestion.queue import IngestQueueUnavailable, enqueue
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db import get_session
from app.authz import Action, ApiError, Resource, require
from app.authz.principal import get_principal

router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/documents", tags=["documents"]
)


class JobAccepted(BaseModel):
    job_id: str
    document_id: str
    version_id: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    document_id: str
    status: str
    stage: str
    attempts: int
    max_attempts: int
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


def _http_error(exc: ApiError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "message": exc.message},
    )


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail={
            "error": "document_too_large",
            "message": "File exceeds the maximum allowed size.",
        },
    )


def _clamp(value: str | None, limit: int = 512) -> str | None:
    if value is None:
        return None
    return value[:limit]


async def _read_limited(upload: UploadFile, limit: int) -> bytes:
    """Stream-read at most ``limit`` bytes; reject 413 as soon as it is exceeded."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAccepted,
    summary="Upload a document and queue an ingestion job",
)
async def upload_document(
    request: Request,
    workspace_id: uuid.UUID,
    file: UploadFile = File(...),
    charset: str | None = Form(default=None),
    title: str | None = Form(default=None),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> JobAccepted:
    try:
        require(principal, Action.DOCUMENT_WRITE, Resource(workspace_id=str(workspace_id)))
    except ApiError as exc:
        raise _http_error(exc) from exc

    # Advisory Content-Length fence (avoid reading a body we already know is big).
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_DOCUMENT_BYTES:
        raise _too_large()

    if charset:
        try:
            codecs.lookup(charset)
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_charset", "message": "Unknown source charset."},
            ) from exc

    # Stream-read with the fence applied during the read (Starlette 1.7 has no
    # UploadFile.size), so an oversized body never fully materializes.
    data = await _read_limited(file, MAX_DOCUMENT_BYTES)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "empty_document", "message": "Uploaded file is empty."},
        )

    checksum = hashlib.sha256(data).hexdigest()
    document = Document(
        workspace_id=workspace_id,
        title=_clamp(title) or _clamp(file.filename),
        source_filename=_clamp(file.filename),
        mime_type=file.content_type,
    )
    session.add(document)
    await session.flush()

    version = DocumentVersion(
        document_id=document.id,
        version_number=1,
        checksum=checksum,
        size_bytes=len(data),
        mime_type=file.content_type,
        charset=charset,
        content=data,
    )
    session.add(version)
    await session.flush()

    job = IngestionJob(
        workspace_id=workspace_id,
        document_id=document.id,
        document_version_id=version.id,
        status=JobStatus.QUEUED.value,
        stage=JobStage.QUEUED.value,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    try:
        await enqueue(str(job.id))
    except IngestQueueUnavailable as exc:
        job.status = JobStatus.FAILED.value
        job.stage = JobStage.FAILED.value
        job.error_code = "queue_unavailable"
        job.error_message = "ingestion queue unavailable"
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "queue_unavailable",
                "message": "Ingestion queue is unavailable; try again shortly.",
            },
        ) from exc

    return JobAccepted(
        job_id=str(job.id),
        document_id=str(document.id),
        version_id=str(version.id),
        status=job.status,
    )


@router.get(
    "/{job_id}",
    response_model=JobStatusResponse,
    summary="Poll an ingestion job's status/stage",
)
async def get_job(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> JobStatusResponse:
    try:
        require(principal, Action.DOCUMENT_READ, Resource(workspace_id=str(workspace_id)))
    except ApiError as exc:
        raise _http_error(exc) from exc

    job = await session.get(IngestionJob, job_id)
    if job is None or job.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "job_not_found", "message": "Ingestion job not found."},
        )
    return JobStatusResponse(
        job_id=str(job.id),
        document_id=str(job.document_id),
        status=job.status,
        stage=job.stage,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        error_code=job.error_code,
        error_message=job.error_message,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )
