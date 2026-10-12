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
from dataclasses import dataclass, field
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
    #: TASK-Q1: explicit partial/degraded disclosure — True when an optional
    #: stage (e.g. graph extraction) failed while mandatory writes succeeded.
    partial: bool = False
    warnings: list[str] = field(default_factory=list)


def ensure_element_limit(elements: list[DocumentElement], limit: int | None = None) -> None:
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

    # TASK-Q1 (V1-COMPLETION-PLAN §3): READY is gated on *verified* mandatory
    # store writes — a call that merely returns without raising is not proof
    # that anything landed. Both flags must be True before JobStatus.READY.
    fts_write_verified = False
    vector_write_verified = False
    #: Explicit partial/degraded disclosures (existing columns only, no migration).
    partial_reasons: list[str] = []

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
        indexed = await lexical_store.index(records=records)
        if indexed != len(records):
            raise IngestPipelineError(
                "INDEX_COUNT_MISMATCH",  # Q0 taxonomy code (packages/core/errors.py)
                f"Postgres FTS reported {indexed} of {len(records)} records written",
                retryable=True,
            )
        try:
            fts_verified = await lexical_store.count_ids(ids=[r.id for r in records])
        except Exception as exc:
            raise IngestPipelineError(
                "STORE_WRITE_UNVERIFIED",  # Q0 taxonomy code (packages/core/errors.py)
                f"Postgres FTS write could not be verified: {type(exc).__name__}: {exc}",
                retryable=True,
            ) from exc
        if fts_verified != len(records):
            raise IngestPipelineError(
                "INDEX_COUNT_MISMATCH",  # Q0 taxonomy code (packages/core/errors.py)
                f"Postgres FTS verified {fts_verified} of {len(records)} written records",
                retryable=True,
            )
        fts_write_verified = True
        logger.info(
            "store_write_verified",
            extra={
                "event": "store_write_verified",
                "job_id": str(job.id),
                "store": "fts",
                "written": len(records),
                "verified": fts_verified,
            },
        )
    except IngestPipelineError:
        raise
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
        from retrieval.adapters.infinity_embed import InfinityEmbeddingProvider, _is_zero_vector
        from retrieval.adapters.qdrant_store import QdrantVectorStore

        coll = "knowledge"
        embed_provider = InfinityEmbeddingProvider()
        qdrant = QdrantVectorStore(collection=coll)
        try:
            await qdrant.ensure_collection(collection=coll, dimension=embed_provider.dimension)

            texts = [c.text for c in result.chunks]
            try:
                vectors = await embed_provider.embed_documents(texts)
            except Exception as exc:
                logger.error(
                    "embedding_failed", extra={"job_id": str(job.id), "error": str(exc)}
                )
                raise IngestPipelineError(
                    "EMBED_UNAVAILABLE",  # Q0 taxonomy code (packages/core/errors.py)
                    f"Embedding provider unavailable: {type(exc).__name__}: {exc}",
                    retryable=True,
                ) from exc

            # Defensive write-side guard (TASK-Q1): never upsert a manufactured
            # embedding. Reuses the provider's zero-vector definition/tolerance
            # (no second implementation); the provider's own HTTP-path checks
            # live in infinity_embed.py.
            if len(vectors) != len(texts):
                raise IngestPipelineError(
                    "EMBED_UNAVAILABLE",  # Q0 taxonomy code (packages/core/errors.py)
                    f"embedding returned {len(vectors)} vectors for {len(texts)} chunks",
                    retryable=True,
                )
            for position, vec in enumerate(vectors):
                if not vec or _is_zero_vector(vec):
                    raise IngestPipelineError(
                        "ZERO_VECTOR_REJECTED",  # Q0 taxonomy code (packages/core/errors.py)
                        f"chunk {position} produced a zero/empty vector; "
                        "refusing to upsert a manufactured embedding",
                        retryable=False,
                    )

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
            try:
                upserted = await qdrant.upsert(collection=coll, records=qdrant_records)
            except Exception as exc:
                logger.error(
                    "qdrant_upsert_failed",
                    extra={"job_id": str(job.id), "error": str(exc)},
                )
                raise IngestPipelineError(
                    "qdrant_indexing_failed",
                    f"Vector store upsert failed: {exc}",
                    retryable=True,
                ) from exc
            if upserted != len(qdrant_records):
                raise IngestPipelineError(
                    "INDEX_COUNT_MISMATCH",  # Q0 taxonomy code (packages/core/errors.py)
                    f"Qdrant reported {upserted} of {len(qdrant_records)} records written",
                    retryable=True,
                )
            try:
                vector_verified = await qdrant.count_ids(
                    collection=coll, ids=[r.id for r in qdrant_records]
                )
            except Exception as exc:
                raise IngestPipelineError(
                    "STORE_WRITE_UNVERIFIED",  # Q0 taxonomy code (packages/core/errors.py)
                    f"Qdrant write could not be verified: {type(exc).__name__}: {exc}",
                    retryable=True,
                ) from exc
            if vector_verified != len(qdrant_records):
                raise IngestPipelineError(
                    "INDEX_COUNT_MISMATCH",  # Q0 taxonomy code (packages/core/errors.py)
                    f"Qdrant verified {vector_verified} of {len(qdrant_records)} written records",
                    retryable=True,
                )
            vector_write_verified = True
            logger.info(
                "store_write_verified",
                extra={
                    "event": "store_write_verified",
                    "job_id": str(job.id),
                    "store": "qdrant",
                    "written": len(qdrant_records),
                    "verified": vector_verified,
                },
            )
        finally:
            await qdrant.close()
    except IngestPipelineError:
        raise
    except Exception as exc:
        logger.error("qdrant_embedding_failed", extra={"job_id": str(job.id), "error": str(exc)})
        raise IngestPipelineError(
            "qdrant_indexing_failed",
            f"Embedding and vector store indexing failed: {exc}",
            retryable=True,
        ) from exc

    # Optional Graph Extraction & Indexing (V2 / RP-04 / QF-04)
    graph_mode_val = os.environ.get("GRAPH_MODE", "off").strip().lower()
    if graph_mode_val in ("rule", "llm"):
        try:
            from graph.extractor import GraphChunk, RuleBasedGraphExtractor
            from graph.factory import graph_db_path
            from graph.sqlite_store import SQLiteGraphStore

            graph_store = SQLiteGraphStore(graph_db_path())
            extractor = RuleBasedGraphExtractor()
            graph_chunks = [
                GraphChunk(
                    chunk_id=str(id_map[c.id]),
                    text=c.text,
                    workspace_id=str(job.workspace_id),
                    document_id=str(document.id) if document else None,
                    page=c.page,
                    section=c.section,
                )
                for c in result.chunks
            ]
            extraction_result = await extractor.extract(graph_chunks)
            if extraction_result.entities:
                await graph_store.upsert_entities(str(job.workspace_id), extraction_result.entities)
            if extraction_result.relationships:
                await graph_store.upsert_relationships(
                    str(job.workspace_id), extraction_result.relationships
                )
            if extraction_result.communities:
                await graph_store.upsert_communities(
                    str(job.workspace_id), extraction_result.communities
                )
            logger.info(
                "graph_extraction_indexed",
                extra={
                    "job_id": str(job.id),
                    "entities": len(extraction_result.entities),
                    "relationships": len(extraction_result.relationships),
                    "communities": len(extraction_result.communities),
                },
            )
        except Exception as exc:
            logger.warning(
                "graph_extraction_failed",
                extra={"job_id": str(job.id), "error": str(exc)},
            )
            # TASK-Q1: partial must be explicit, never a silent ready (V1 §5
            # "no silent mandatory failures"). Disclosed through the existing
            # error_code/error_message columns only (no migration) — the API
            # surfaces them on an otherwise READY job via `_job_error`.
            # TASK-Q1 partial reason — Q0 taxonomy code requested in handoff
            # (packages/core/errors.py)
            job.error_code = "GRAPH_INDEX_PARTIAL"
            job.error_message = (
                f"graph_extraction_failed: {type(exc).__name__}: {exc}"
            )[:2000]
            partial_reasons.append(f"graph_extraction_failed: {type(exc).__name__}: {exc}")

    # TASK-Q1 verified-write gate: READY requires every mandatory store write
    # (Postgres FTS + Qdrant) to have been re-read and counted successfully.
    if not (fts_write_verified and vector_write_verified):
        raise IngestPipelineError(
            "STORE_WRITE_UNVERIFIED",  # Q0 taxonomy code (packages/core/errors.py)
            "refusing READY: mandatory store writes are unverified "
            f"(fts={fts_write_verified}, vectors={vector_write_verified})",
            retryable=False,
        )

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
            "partial": bool(partial_reasons),
        },
    )
    if partial_reasons:
        logger.warning(
            "ingest_job_ready_partial",
            extra={
                "event": "ingest_job_ready_partial",
                "job_id": str(job.id),
                "warnings": partial_reasons,
            },
        )
    return PipelineResult(
        job_id=str(job.id),
        chunk_count=len(result.chunks),
        language=result.language,
        stages=stages,
        partial=bool(partial_reasons),
        warnings=list(partial_reasons),
    )
