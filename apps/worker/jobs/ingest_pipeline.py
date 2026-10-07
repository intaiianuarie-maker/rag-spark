"""Ingestion pipeline (M2-001c): parse → guards → chunk → persist → ready.

Runs only in the worker (never in the API process). Deterministic input problems
are typed and non-retryable; unexpected/transient failures are retryable so the
worker can back off and eventually park a poison message as failed.

Guards applied before any chunk/DB write:
- size cap (``MAX_DOCUMENT_BYTES``);
- **element-count cap** (boss ruling: a 25 MiB pathological TXT can yield 1.46M
  elements — refuse past N with a typed error before Postgres writes).
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from ingestion.adapters.registry import parser_for_mime
from ingestion.chunk import chunk_document
from ingestion.interfaces import DocumentElement, ParsedDocument
from ingestion.normalize import MAX_DOCUMENT_BYTES, IngestionError
from ingestion.persistence import (
    ChunkRow,
    Document,
    DocumentVersion,
    IngestionJob,
    JobStage,
    JobStatus,
)
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("knowledge_os.worker.ingest")

DEFAULT_MAX_ELEMENTS = 100_000


def max_elements() -> int:
    try:
        return int(os.environ.get("INGEST_MAX_ELEMENTS", str(DEFAULT_MAX_ELEMENTS)))
    except ValueError:
        return DEFAULT_MAX_ELEMENTS


class IngestPipelineError(Exception):
    """Typed pipeline failure; ``retryable`` drives the worker retry policy."""

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class PipelineResult:
    job_id: str
    chunk_count: int
    language: str
    stages: list[str]


def ensure_element_limit(
    elements: list[DocumentElement], limit: int | None = None
) -> None:
    """Refuse a document that normalizes to more than ``limit`` elements."""
    effective = max_elements() if limit is None else limit
    if len(elements) > effective:
        raise IngestPipelineError(
            "too_many_elements",
            f"document produced {len(elements)} elements (limit {effective})",
            retryable=False,
        )


async def process_job(session: AsyncSession, job_id: uuid.UUID) -> PipelineResult:
    job = await session.get(IngestionJob, job_id)
    if job is None:
        raise IngestPipelineError("job_not_found", "ingestion job not found", retryable=False)
    version = await session.get(DocumentVersion, job.document_version_id)
    if version is None:
        raise IngestPipelineError("version_not_found", "document version missing", retryable=False)
    document = await session.get(Document, job.document_id)
    content = version.content or b""

    job.status = JobStatus.RUNNING.value
    job.stage = JobStage.PARSING.value
    job.attempts += 1
    job.started_at = datetime.now(UTC)
    job.error_code = None
    job.error_message = None
    await session.commit()
    stages = [JobStage.PARSING.value]

    if not content:
        raise IngestPipelineError("empty_document", "empty document", retryable=False)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise IngestPipelineError("document_too_large", "document too large", retryable=False)

    parser = parser_for_mime(version.mime_type)
    if parser is None:
        raise IngestPipelineError(
            "unsupported_mime_type",
            f"unsupported mime type: {version.mime_type}",
            retryable=False,
        )
    mime_type = version.mime_type or ""
    parse_metadata = {"charset": version.charset} if version.charset else None

    try:
        parsed: ParsedDocument = await parser.parse(
            content,
            mime_type=mime_type,
            filename=document.source_filename if document else None,
            metadata=parse_metadata,
        )
        ensure_element_limit(parsed.elements)  # BEFORE chunk/DB writes
    except IngestPipelineError:
        raise
    except IngestionError as exc:
        raise IngestPipelineError(exc.code, exc.message, retryable=False) from exc
    except Exception as exc:  # unexpected/transient -> retryable
        raise IngestPipelineError(
            "parse_failed", f"{type(exc).__name__}: {exc}", retryable=True
        ) from exc

    job.stage = JobStage.CHUNKING.value
    await session.commit()
    stages.append(JobStage.CHUNKING.value)

    try:
        result = chunk_document(
            parsed, filename=document.source_filename if document else None
        )
    except Exception as exc:
        raise IngestPipelineError(
            "chunk_failed", f"{type(exc).__name__}: {exc}", retryable=True
        ) from exc

    await session.execute(delete(ChunkRow).where(ChunkRow.document_version_id == version.id))
    id_map: dict[str, uuid.UUID] = {}
    for chunk in result.chunks:
        row_id = uuid.uuid4()
        id_map[chunk.id] = row_id
        session.add(
            ChunkRow(
                id=row_id,
                document_version_id=version.id,
                workspace_id=job.workspace_id,
                chunk_index=chunk.index,
                text=chunk.text,
                token_count=chunk.token_count,
                parent_id=id_map.get(chunk.parent_id) if chunk.parent_id else None,
                section=chunk.section,
                page=chunk.page,
                language=result.language,
                chunk_metadata=chunk.metadata,
            )
        )
    if document is not None:
        document.language = result.language
        if document.title is None:
            document.title = result.metadata.title

    # M3 wires the real embedding/indexing adapters; recorded as explicit no-op
    # stages so the transition sequence is observable now.
    job.stage = JobStage.EMBEDDING.value
    await session.commit()
    stages.append(JobStage.EMBEDDING.value)

    job.stage = JobStage.INDEXING.value
    await session.commit()
    stages.append(JobStage.INDEXING.value)

    job.status = JobStatus.READY.value
    job.stage = JobStage.READY.value
    job.finished_at = datetime.now(UTC)
    await session.commit()
    stages.append(JobStage.READY.value)

    logger.info(
        "ingest_job_ready",
        extra={
            "event": "ingest_job_ready",
            "job_id": str(job.id),
            "chunks": len(result.chunks),
            "language": result.language,
        },
    )
    return PipelineResult(
        job_id=str(job.id),
        chunk_count=len(result.chunks),
        language=result.language,
        stages=stages,
    )
