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

from ingestion.adapters.llamaindex_chunker import LlamaIndexUnavailableError
from ingestion.adapters.llamaindex_chunker import (
    backend_name as llamaindex_backend_name,
)
from ingestion.adapters.llamaindex_chunker import (
    chunk_document as llamaindex_chunk_document,
)
from ingestion.adapters.registry import parser_for_mime
from ingestion.chunk import ChunkResult, chunk_document
from ingestion.dedup import supersede_other_versions
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


def _log_stage(job: IngestionJob, stage: str) -> None:
    """Micro #5: emit a greppable stage token for each transition."""
    logger.info(
        "ingest_stage",
        extra={"event": "ingest_stage", "job_id": str(job.id), "stage": stage},
    )


# Chunking backend selection (rag-fork-solution TASK-M2-002): LlamaIndex
# sentence splitter by default, built-in whitespace chunker on
# CHUNKING_BACKEND=builtin. Default falls back to the built-in chunker with a
# loud warning when llama-index-core is missing from the image, so ingestion
# never hard-fails on an optional dependency.
CHUNKING_BACKEND = os.environ.get("CHUNKING_BACKEND", "llamaindex").strip().lower()


def resolved_chunking_backend() -> str:
    """Backend identifier actually in use (recorded in stage logs + debugger)."""
    if CHUNKING_BACKEND == "builtin":
        return "builtin-whitespace"
    try:
        import llama_index.core  # noqa: F401  - availability probe only
    except ImportError:
        return "builtin-whitespace"
    return llamaindex_backend_name()


def _chunk(parsed: ParsedDocument, *, filename: str | None) -> ChunkResult:
    """Chunk with the selected backend (both emit the identical Chunk model)."""
    if CHUNKING_BACKEND == "builtin":
        return chunk_document(parsed, filename=filename)
    try:
        return llamaindex_chunk_document(parsed, filename=filename)
    except LlamaIndexUnavailableError as exc:
        logger.warning(
            "chunking_backend_fallback",
            extra={"event": "chunking_backend_fallback", "reason": str(exc)},
        )
        return chunk_document(parsed, filename=filename)


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
    _log_stage(job, JobStage.PARSING.value)

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
    _log_stage(job, JobStage.CHUNKING.value)
    logger.info(
        "chunking_backend",
        extra={
            "event": "chunking_backend",
            "requested": CHUNKING_BACKEND,
            "resolved": resolved_chunking_backend(),
        },
    )

    try:
        result = _chunk(parsed, filename=document.source_filename if document else None)
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
        await supersede_other_versions(session, document.id, version.id)
        document.language = result.language
        if document.title is None:
            document.title = result.metadata.title

    # Stage: INDEXING (Postgres FTS)
    job.stage = JobStage.INDEXING.value
    await session.commit()
    stages.append(JobStage.INDEXING.value)
    _log_stage(job, JobStage.INDEXING.value)

    try:
        from retrieval.adapters.pg_fts import PostgresLexicalStore
        from retrieval.interfaces import VectorRecord
        lexical_store = PostgresLexicalStore()
        records = [
            VectorRecord(
                id=str(id_map[c.id]),
                workspace_id=str(job.workspace_id),
                document_id=str(document.id) if document else None,
                document_version_id=str(version.id),
                chunk_id=str(id_map[c.id]),
                content=c.text,
                page=c.page,
                section=c.section,
                metadata={"language": result.language, **(c.metadata or {})},
            )
            for c in result.chunks
        ]
        await lexical_store.index(records=records)
    except Exception as exc:
        logger.error("fts_indexing_failed", extra={"job_id": str(job.id), "error": str(exc)})
        raise IngestPipelineError(
            "fts_indexing_failed",
            f"Postgres FTS indexing failed: {exc}",
            retryable=True,
        ) from exc

    # Stage: EMBEDDING (Qdrant vector store)
    job.stage = JobStage.EMBEDDING.value
    await session.commit()
    stages.append(JobStage.EMBEDDING.value)
    _log_stage(job, JobStage.EMBEDDING.value)

    try:
        from retrieval.adapters.infinity_embed import InfinityEmbeddingProvider
        from retrieval.adapters.qdrant_store import QdrantVectorStore

        coll = "knowledge"
        qdrant = QdrantVectorStore(collection=coll)
        await qdrant.ensure_collection(collection=coll, dimension=1024)

        embed_provider = InfinityEmbeddingProvider()
        texts = [c.text for c in result.chunks]
        vectors = await embed_provider.embed_documents(texts)

        qdrant_records = [
            VectorRecord(
                id=str(id_map[c.id]),
                vector=vec,
                workspace_id=str(job.workspace_id),
                document_id=str(document.id) if document else None,
                document_version_id=str(version.id),
                chunk_id=str(id_map[c.id]),
                content=c.text,
                page=c.page,
                section=c.section,
                metadata={"language": result.language, **(c.metadata or {})},
            )
            for c, vec in zip(result.chunks, vectors, strict=True)
        ]
        await qdrant.upsert(collection=coll, records=qdrant_records)
        await qdrant.close()
    except Exception as exc:
        logger.error("qdrant_embedding_failed", extra={"job_id": str(job.id), "error": str(exc)})
        raise IngestPipelineError(
            "qdrant_indexing_failed",
            f"Embedding and vector store indexing failed: {exc}",
            retryable=True,
        ) from exc

    job.status = JobStatus.READY.value
    job.stage = JobStage.READY.value
    job.finished_at = datetime.now(UTC)
    await session.commit()
    stages.append(JobStage.READY.value)
    _log_stage(job, JobStage.READY.value)

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
