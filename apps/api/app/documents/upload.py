"""Document upload + ingestion-job status (M2-001c).

The API never parses: it stores the original bytes **in object storage**
(S3/SeaweedFS behind the storage seam — Postgres keeps only ``storage_key`` +
``checksum``, T-V1-039 / PD-005), creates a ``queued`` ingestion job, enqueues
the job id on Redis, and returns in one round-trip. Parsing/chunking happen in
the worker, which reads the original back through the same seam.
"""

from __future__ import annotations

import codecs
import hashlib
import logging
import os
import uuid
from datetime import UTC, datetime

from core.auth import Principal
from core.errors import ReasonCode, error_detail
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from ingestion.adapters.registry import parser_for_mime
from ingestion.dedup import find_document_by_filename, latest_job_for_version, latest_version
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
from storage.interfaces import StorageError
from storage.originals import OriginalChecksumMismatch, put_original

from app.auth.db import get_session
from app.authz import Action, ApiError, Resource, require
from app.authz.principal import get_principal

logger = logging.getLogger("knowledge_os.api.documents")

router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/documents", tags=["documents"]
)

# TASK-M2-003 freed `/documents/{id}` for the frozen document-detail read, so the
# pre-existing job poll (M2-001c) moved one level up: `/jobs/{job_id}`. Same
# handler/response; only the path changed.
jobs_router = APIRouter(prefix="/v1/workspaces/{workspace_id}/jobs", tags=["ingestion"])


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


# Filename-extension fallback for uploads that arrive with no (or a generic)
# Content-Type. Only types the parser registry already serves are guessed.
_EXT_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
}


def _resolve_mime(content_type: str | None, filename: str | None) -> str | None:
    """Declared type (parameters stripped), else a supported extension guess, else None."""
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared and declared != "application/octet-stream":
        return declared
    guess = _EXT_MIME.get(os.path.splitext(filename or "")[1].lower())
    return guess if guess and parser_for_mime(guess) else None


def _clamp(value: str | None, limit: int = 512) -> str | None:
    if value is None:
        return None
    return value[:limit]


async def _enqueue_or_fail(session: AsyncSession, job: IngestionJob) -> None:
    """Enqueue a queued job; mark it failed and 503 if the queue is unreachable."""
    try:
        await enqueue(str(job.id))
    except IngestQueueUnavailable as exc:
        job.status = JobStatus.FAILED.value
        job.stage = JobStage.FAILED.value
        job.error_code = "QUEUE_UNAVAILABLE"
        job.error_message = "ingestion queue unavailable"
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "QUEUE_UNAVAILABLE",
                "message": "Ingestion queue is unavailable; try again shortly.",
            },
        ) from exc


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAccepted,
    summary="Upload a document and queue an ingestion job",
)
async def upload_document(
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

    if charset:
        try:
            codecs.lookup(charset)
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_CHARSET", "message": "Unknown source charset."},
            ) from exc

    data = await file.read()
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "EMPTY_DOCUMENT", "message": "Uploaded file is empty."},
        )

    filename = _clamp(file.filename)
    mime_type = _resolve_mime(file.content_type, filename)
    if mime_type is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=error_detail(ReasonCode.UNSUPPORTED_MIME_TYPE),
        )

    checksum = hashlib.sha256(data).hexdigest()
    existing = await find_document_by_filename(session, workspace_id, filename)

    if existing is not None:
        latest = await latest_version(session, existing.id)
        if latest is not None and latest.checksum == checksum:
            latest_job = await latest_job_for_version(session, latest.id)
            if latest_job is not None and latest_job.status in {
                JobStatus.QUEUED.value,
                JobStatus.RUNNING.value,
            }:
                # In-flight: report the existing job honestly; do not stack one.
                return JobAccepted(
                    job_id=str(latest_job.id),
                    document_id=str(existing.id),
                    version_id=str(latest.id),
                    status=latest_job.status,
                )
            if latest_job is None or latest_job.status != JobStatus.READY.value:
                # Previous attempt failed (or never ran): re-parse the same version
                # rather than reporting a dishonest `ready` with zero chunks.
                # Refresh the mime: a poisoned (e.g. untyped) first upload must
                # not be reused as-is.
                latest.mime_type = mime_type
                job = IngestionJob(
                    workspace_id=workspace_id,
                    document_id=existing.id,
                    document_version_id=latest.id,
                    status=JobStatus.QUEUED.value,
                    stage=JobStage.QUEUED.value,
                )
                session.add(job)
                await session.commit()
                await session.refresh(job)
                await _enqueue_or_fail(session, job)
                return JobAccepted(
                    job_id=str(job.id),
                    document_id=str(existing.id),
                    version_id=str(latest.id),
                    status=job.status,
                )
            # Only reuse a version whose ingestion actually reached `ready`.
            job = IngestionJob(
                workspace_id=workspace_id,
                document_id=existing.id,
                document_version_id=latest.id,
                status=JobStatus.READY.value,
                stage=JobStage.READY.value,
                finished_at=datetime.now(UTC),
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)
            return JobAccepted(
                job_id=str(job.id),
                document_id=str(existing.id),
                version_id=str(latest.id),
                status=job.status,
            )
        document = existing
        version_number = (latest.version_number if latest else 0) + 1
    else:
        document = Document(
            workspace_id=workspace_id,
            title=_clamp(title) or filename,
            source_filename=filename,
            mime_type=mime_type,
        )
        session.add(document)
        await session.flush()
        version_number = 1

    # Originals go to object storage BEFORE the row commits: an object with no
    # row is inert, a row with no object is a document that can never be parsed
    # again (T-V1-039). Both refusals are storage-side, never a client error,
    # and their messages come from the registry so no vendor/endpoint text can
    # reach a response (AGENTS safe-user-reason).
    version_id = uuid.uuid4()
    try:
        storage_key = await put_original(
            workspace_id=workspace_id,
            document_id=document.id,
            version_id=version_id,
            data=data,
            checksum=checksum,
            filename=filename,
        )
    except OriginalChecksumMismatch as exc:
        logger.warning(
            "original_integrity_failure",
            extra={"event": "original_integrity_failure", "document_id": str(document.id)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_detail(ReasonCode.INTERNAL_ERROR),
        ) from exc
    except StorageError as exc:
        logger.warning(
            "original_store_unavailable",
            extra={"event": "original_store_unavailable", "document_id": str(document.id)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error_detail(ReasonCode.DEPENDENCY_UNAVAILABLE),
        ) from exc

    version = DocumentVersion(
        id=version_id,
        document_id=document.id,
        version_number=version_number,
        checksum=checksum,
        size_bytes=len(data),
        mime_type=mime_type,
        charset=charset,
        storage_key=storage_key,
    )
    session.add(version)
    await session.flush()
    document.current_version_id = version.id

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

    await _enqueue_or_fail(session, job)

    return JobAccepted(
        job_id=str(job.id),
        document_id=str(document.id),
        version_id=str(version.id),
        status=job.status,
    )


@jobs_router.get(
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
            detail={"error": "JOB_NOT_FOUND", "message": "Ingestion job not found."},
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
