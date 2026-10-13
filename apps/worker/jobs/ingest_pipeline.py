"""Ingestion pipeline (M2-001c): parse → guards → chunk → persist → ready.

Runs only in the worker (never in the API process). Deterministic input problems
are typed and non-retryable; unexpected/transient failures are retryable so the
worker can back off and eventually park a poison message as failed.

Guards applied before any chunk/DB write:
- size cap (``MAX_DOCUMENT_BYTES``);
- **pre-expansion parse budgets** (T-V1-031): parse wall clock, peak memory,
  expanded-text and output-element budgets are enforced *while* the parser
  expands the document (see :mod:`ingestion.budget`), not by counting elements
  after the fact — a document that would blow a budget is refused between
  elements and parked non-retryably;
- **element-count cap** (boss ruling: a 25 MiB pathological TXT can yield 1.46M
  elements — refuse past N with a typed error before Postgres writes; kept as
  the independent post-parse backstop);
- **generation fence** (T-V1-019): a job is bound to one document generation
  and may only publish it while that generation is still the document's live
  one — see :func:`assert_generation_is_publishable`.

T-V1-031 honesty rule: a PDF page that is an image with no text layer is a
**lost region**. Nothing is invented for it, the lost pages are disclosed on
every chunk, and the job ends in ``needs_review`` — never in ``ready``.
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
from ingestion.adapters.pdf_scan import pdf_text_layer_report
from ingestion.adapters.registry import (
    ParseReport,
    parse_with_precedence,
    parser_for_mime,
)
from ingestion.budget import ParseBudgetExceeded, budgets_from_env
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
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from storage.interfaces import StorageError
from storage.originals import (
    OriginalChecksumMismatch,
    OriginalMissing,
    load_original,
)

logger = logging.getLogger("knowledge_os.worker.ingest")

DEFAULT_MAX_ELEMENTS = 100_000

#: T-V1-016: UUID namespace for deterministic publication IDs. A document
#: *generation* is one ``DocumentVersion`` row (a new upload mints a new
#: version → new generation); the *active* version is the pointer
#: ``Document.current_version_id``. Retries of the same job re-run inside the
#: same generation and must reuse identical chunk IDs, so FTS/Qdrant writes
#: overwrite in place instead of accumulating orphan vector IDs.
PUBLICATION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "knowledge-os/publication")


def stable_chunk_id(version_id: uuid.UUID, chunk_id: str) -> uuid.UUID:
    """Deterministic chunk publication ID within a document generation (T-V1-016).

    Derived from (generation, chunk id) — both stable across retries of the
    same job (the chunker emits positional ``chunk-NNNN`` ids). A new
    generation (new version/upload) yields entirely fresh IDs.
    """
    return uuid.uuid5(PUBLICATION_NAMESPACE, f"{version_id}:{chunk_id}")


# T-V1-019: old-job fencing --------------------------------------------------#
#: A job is bound to ONE generation (its ``document_version_id``). While it
#: runs, the document may be **replaced** (a newer upload points
#: ``Document.current_version_id`` elsewhere) or **deleted** (``deleted_at``
#: tombstone). A job that finishes late must not resurrect its generation or
#: mark itself READY, so every publication boundary re-compares the job's
#: generation against the authoritative document row. The codes are the
#: non-retryable failure reason left on the job (retrying a dead generation can
#: never make it live again); they pass through ``normalize_code`` unchanged.
FENCE_SUPERSEDED = "STALE_GENERATION"
FENCE_DELETED = "DOCUMENT_DELETED"
FENCE_GONE = "GENERATION_GONE"

_FENCE_MESSAGES: dict[str, str] = {
    FENCE_SUPERSEDED: "a newer generation of this document superseded this job's",
    FENCE_DELETED: "this document was deleted while the job was running",
    FENCE_GONE: "this document generation no longer exists",
}


async def generation_publication_state(
    session: AsyncSession, *, document_id: uuid.UUID, version_id: uuid.UUID
) -> str | None:
    """May this generation still publish? ``None`` = yes, else the fence reason.

    T-V1-019's authority is read **fresh** from ``documents`` /
    ``document_versions`` — deliberately not the session's identity map, whose
    copy was taken at dequeue time and is exactly the state that can be stale.
    The comparison is a pure generation compare (pointer, tombstone,
    ``superseded_at``, version number): no clock, no sleep, no guess about
    whether async cleanup has run yet (ticket checklist 3).

    ``Document.current_version_id`` is the same authority T-V1-018's read gate
    validates payloads against, so the write side and the read side fence on
    one definition of "the live generation":

    * ``deleted_at`` set → :data:`FENCE_DELETED` (delete case);
    * ``current_version_id`` set to another version, or ``superseded_at`` set →
      :data:`FENCE_SUPERSEDED` (replace case);
    * the version row is gone → :data:`FENCE_GONE`;
    * pointer never set (pre-migration rows and directly-constructed fixtures):
      no claim is made by the pointer, so the fallback generation compare asks
      whether a strictly newer version of the same document exists — mirroring
      T-V1-018's "no claim ⇒ not stale" rule rather than stranding those rows.
    """
    row = (
        await session.execute(
            select(
                Document.deleted_at,
                Document.current_version_id,
                DocumentVersion.superseded_at,
                DocumentVersion.version_number,
            )
            .select_from(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentVersion.id == version_id)
        )
    ).first()
    if row is None:
        return FENCE_GONE
    deleted_at, current_version_id, superseded_at, version_number = row
    if deleted_at is not None:
        return FENCE_DELETED
    if superseded_at is not None or (
        current_version_id is not None and current_version_id != version_id
    ):
        return FENCE_SUPERSEDED
    if current_version_id is None:
        newer = await session.scalar(
            select(func.count())
            .select_from(DocumentVersion)
            .where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.version_number > version_number,
            )
        )
        return FENCE_SUPERSEDED if newer else None
    return None


async def _unpublish_generation(
    session: AsyncSession, *, document_id: uuid.UUID, version_id: uuid.UUID
) -> None:
    """Take one dead generation back out of every store (T-V1-019 compensation).

    Called only once the fence has refused a job: whatever this run already
    wrote belongs to a generation that is no longer published, so it must leave
    Postgres chunks, the FTS rows and the vector points even when an earlier
    stage of the same run committed them. The delete is scoped to this
    ``document_version_id`` only — no other generation is touched.

    The worker rolls the pipeline session back when the fence raises, so this
    cleanup **commits before that raise**: an unpublish riding the doomed
    transaction would be undone by the rollback and the stale generation would
    silently survive. A store that cannot be reached is logged and skipped —
    the fence refuses the job regardless, and the log is the alarm.
    """
    try:
        await session.execute(
            delete(ChunkRow).where(ChunkRow.document_version_id == version_id)
        )
        await session.execute(
            text("DELETE FROM lexical_docs WHERE document_version_id = :version_id"),
            {"version_id": str(version_id)},
        )
        try:
            from retrieval.adapters.qdrant_store import QdrantVectorStore

            qdrant = QdrantVectorStore(collection="knowledge")
            try:
                await qdrant.delete_versions(
                    collection="knowledge",
                    version_ids=[str(version_id)],
                    document_id=str(document_id),
                )
            finally:
                await qdrant.close()
        except Exception as exc:  # best effort: the job is refused either way
            logger.warning(
                "fenced_generation_vector_cleanup_failed",
                extra={
                    "event": "fenced_generation_vector_cleanup_failed",
                    "document_id": str(document_id),
                    "document_version_id": str(version_id),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        await session.commit()
    except Exception as exc:
        logger.error(
            "fenced_generation_unpublish_failed",
            extra={
                "event": "fenced_generation_unpublish_failed",
                "document_id": str(document_id),
                "document_version_id": str(version_id),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


async def assert_generation_is_publishable(
    session: AsyncSession,
    *,
    job: IngestionJob,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    gate: str,
) -> None:
    """The T-V1-019 publication gate: refuse a job whose generation is dead.

    Raises a **non-retryable** :class:`IngestPipelineError` naming the fence
    reason, after undoing this generation's publication. The caller (and the
    worker's ``record_failure``) then park the job as FAILED — it can never
    reach READY, so a late job can neither resurrect its content nor
    false-mark its document as published.
    """
    reason = await generation_publication_state(
        session, document_id=document_id, version_id=version_id
    )
    if reason is None:
        return
    logger.warning(
        "ingest_publication_fenced",
        extra={
            "event": "ingest_publication_fenced",
            "job_id": str(job.id),
            "document_id": str(document_id),
            "document_version_id": str(version_id),
            "gate": gate,
            "error_code": reason,
        },
    )
    await _unpublish_generation(session, document_id=document_id, version_id=version_id)
    raise IngestPipelineError(
        reason,
        f"refusing to publish at the {gate} gate: "
        + _FENCE_MESSAGES.get(reason, "this generation is no longer publishable")
        + f" (document {document_id}, generation {version_id})",
        retryable=False,
    )


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
    #: T-V1-031: True when the content landed but part of the source could not
    #: be read (e.g. image-only PDF pages). The job is parked ``needs_review``.
    needs_review: bool = False
    #: The lost regions themselves (page lists), so a caller never has to
    #: re-derive them from the message text.
    lost_regions: list[dict] = field(default_factory=list)


def ensure_element_limit(elements: list[DocumentElement], limit: int | None = None) -> None:
    """Refuse a document that normalizes to more than ``limit`` elements.

    Kept as an independent **post-parse backstop**: the pre-expansion budgets
    (T-V1-031, :mod:`ingestion.budget`) are the enforcement point, this is the
    belt that catches a parser which produced its elements without ever
    hitting a checkpoint.
    """
    effective = max_elements() if limit is None else limit
    if len(elements) > effective:
        raise IngestPipelineError(
            "TOO_MANY_ELEMENTS",
            f"document produced {len(elements)} elements (limit {effective})",
            retryable=False,
        )


#: Chunk-metadata keys carrying the T-V1-031 parse disclosure. They ride to
#: the answer layer through the stores' metadata passthrough, so an answer over
#: a partially-read source can say so (``NEEDS_REVIEW``) instead of looking
#: confident about the regions that were never read.
PARSE_PARTIAL_KEY = "parse_partial"
PARSE_LOST_REGIONS_KEY = "parse_lost_regions"
PARSE_REPORT_KEY = "parse"


def parse_disclosure(report: ParseReport, lost_regions: list[dict]) -> dict:
    """The disclosure stored on every chunk of one document version."""
    disclosure: dict = {PARSE_REPORT_KEY: report.as_metadata()}
    if lost_regions:
        disclosure[PARSE_PARTIAL_KEY] = True
        disclosure[PARSE_LOST_REGIONS_KEY] = list(lost_regions)
    return disclosure


async def _park_needs_review(
    session: AsyncSession,
    *,
    job: IngestionJob,
    reasons: list[str],
    disclosure: dict,
    stages: list[str],
) -> PipelineResult:
    """Publish nothing, and say why (T-V1-031 image-only document).

    Used when a document cannot be read **at all** (every page is an image with
    no text layer). The honest outcomes are "refused" or "needs review" — the
    dishonest one is indexing an empty document as if it had been read. We park
    the job ``needs_review`` so the source stays visible and reviewable, and no
    chunk is published that an answer could cite as if it covered the file.

    The generation fence still runs first: a dead generation must not be able
    to leave a review request behind either.
    """
    await assert_generation_is_publishable(
        session,
        job=job,
        document_id=job.document_id,
        version_id=job.document_version_id,
        gate="write",
    )
    job.status = JobStatus.NEEDS_REVIEW.value
    job.stage = JobStage.READY.value
    job.error_code = "NEEDS_REVIEW"
    job.error_message = "; ".join(reasons)[:2000]
    job.finished_at = datetime.now(UTC)
    await session.commit()
    stages.append(JobStage.READY.value)
    _log_stage(job, JobStage.READY.value)
    logger.warning(
        "ingest_job_needs_review",
        extra={
            "event": "ingest_job_needs_review",
            "job_id": str(job.id),
            "document_id": str(job.document_id),
            "chunks": 0,
            "warnings": reasons,
            "disclosure": disclosure,
        },
    )
    return PipelineResult(
        job_id=str(job.id),
        chunk_count=0,
        language="unknown",
        stages=stages,
        partial=True,
        warnings=list(reasons),
        needs_review=True,
        lost_regions=list(disclosure.get(PARSE_LOST_REGIONS_KEY, [])),
    )


def needs_review_reasons_for(lost_regions: list[dict]) -> list[str]:
    """Human-readable reasons a partially-read document needs review (T-V1-031)."""
    return [
        f"{len(region['lost_pages'])} of {region['page_count']} page(s) carry an "
        f"image and no text layer (pages {region['lost_pages']}); that content "
        "was NOT read"
        for region in lost_regions
    ]


def terminal_job_state(lost_regions: list[dict]) -> tuple[str, str | None, str | None]:
    """``(status, error_code, error_message)`` for a finished job (T-V1-031).

    Pure, so the honesty rule is pinned directly instead of through a DB run:
    a document that was read in full finishes ``ready``; a document with lost
    regions finishes ``needs_review`` — its readable text is still published
    (retrieval over what *was* read keeps working), but the job never claims
    ``ready`` and always carries the disclosure.

    ``process_job`` derives the same three values inline (the T-V1-019 gate
    test pins the READY transition by source order), so this function is the
    specification the inline branch is checked against by the matrix tests.
    """
    reasons = needs_review_reasons_for(lost_regions)
    if not reasons:
        return JobStatus.READY.value, None, None
    return (
        JobStatus.NEEDS_REVIEW.value,
        "NEEDS_REVIEW",
        ("NEEDS_REVIEW: " + "; ".join(reasons))[:2000],
    )


async def load_original_bytes(version: DocumentVersion) -> bytes:
    """Fetch this version's original bytes from object storage (T-V1-039).

    Postgres keeps ``storage_key`` + ``checksum`` only, so every parse starts
    with a fetch through the storage seam and a SHA-256 gate. Three verdicts,
    and none of them is a guess:

    - **no key** -> ``b""``: a row that never had bytes (pre-object-store rows
      with ``content IS NULL``). The caller's existing ``EMPTY_DOCUMENT`` check
      then refuses it — the same verdict this pipeline gave that state before
      the column moved, so nothing changed for it;
    - **object gone / bytes that do not hash to the row's checksum** ->
      terminal ``INGESTION_ERROR``. Retrying cannot restore bytes that were
      never written or that rotted, so burning the retry budget would only
      delay an honest failure;
    - **store unreachable** -> ``DEPENDENCY_UNAVAILABLE``, retryable — object
      storage is a backing dependency and a blip is a blip.

    Vendor detail (endpoint, host, SDK error) is logged and never makes it
    into ``ingestion_jobs.error_message`` (AGENTS safe-user-reason).
    """
    if not version.storage_key:
        return b""
    try:
        return await load_original(version.storage_key, checksum=version.checksum)
    except OriginalChecksumMismatch as exc:
        logger.warning(
            "ingest_original_checksum_mismatch",
            extra={
                "event": "ingest_original_checksum_mismatch",
                "version_id": str(version.id),
                "error": str(exc),
            },
        )
        raise IngestPipelineError(
            "INGESTION_ERROR",
            "original failed SHA-256 verification against the stored checksum",
            retryable=False,
        ) from exc
    except OriginalMissing as exc:
        logger.warning(
            "ingest_original_missing",
            extra={
                "event": "ingest_original_missing",
                "version_id": str(version.id),
                "error": str(exc),
            },
        )
        raise IngestPipelineError(
            "INGESTION_ERROR",
            "original object is missing from object storage",
            retryable=False,
        ) from exc
    except StorageError as exc:
        logger.warning(
            "ingest_original_unavailable",
            extra={
                "event": "ingest_original_unavailable",
                "version_id": str(version.id),
                "error": str(exc),
            },
        )
        raise IngestPipelineError(
            "DEPENDENCY_UNAVAILABLE",
            "object storage unavailable while reading the original",
            retryable=True,
        ) from exc


async def process_job(session: AsyncSession, job_id: uuid.UUID) -> PipelineResult:
    job = await session.get(IngestionJob, job_id)
    if job is None:
        raise IngestPipelineError("JOB_NOT_FOUND", "ingestion job not found", retryable=False)
    version = await session.get(DocumentVersion, job.document_version_id)
    if version is None:
        raise IngestPipelineError("VERSION_NOT_FOUND", "document version missing", retryable=False)
    document = await session.get(Document, job.document_id)

    job.status = JobStatus.RUNNING.value
    job.stage = JobStage.PARSING.value
    job.attempts += 1
    job.started_at = datetime.now(UTC)
    job.error_code = None
    job.error_message = None
    await session.commit()
    stages = [JobStage.PARSING.value]
    _log_stage(job, JobStage.PARSING.value)

    #: T-V1-039: the original comes from object storage, fetched (and
    #: checksum-verified) only now that the attempt is recorded — an
    #: object-store outage becomes a failure on this job row instead of an
    #: error that never reaches one.
    content = await load_original_bytes(version)

    if not content:
        raise IngestPipelineError("EMPTY_DOCUMENT", "empty document", retryable=False)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise IngestPipelineError("DOCUMENT_TOO_LARGE", "document too large", retryable=False)

    # T-V1-031 explicit-rejection cell (XLSX, images): the refusal happens
    # BEFORE any parser sees the bytes, and it is a typed, advertised answer —
    # see `ingestion.format_matrix` and docs/ingestion/FORMAT-MATRIX.md.
    if parser_for_mime(version.mime_type) is None:
        raise IngestPipelineError(
            "UNSUPPORTED_MIME_TYPE",
            f"unsupported mime type: {version.mime_type}",
            retryable=False,
        )
    mime_type = version.mime_type or ""
    parse_metadata = {"charset": version.charset} if version.charset else None

    #: T-V1-031 lost regions: pages that carry an image and no text layer.
    #: Empty for a fully readable document.
    lost_regions: list[dict] = []
    if mime_type == "application/pdf":
        text_layer = pdf_text_layer_report(content)
        if text_layer is not None and text_layer.fully_lost:
            # Nothing here is readable and nothing may be invented for it.
            return await _park_needs_review(
                session,
                job=job,
                reasons=[
                    "NEEDS_REVIEW: no text layer on any page "
                    f"({len(text_layer.lost_pages)} of {text_layer.page_count} page(s) "
                    "are image-only); OCR is not enabled in this deployment, so the "
                    "content was NOT read"
                ],
                disclosure=text_layer.disclosure(),
                stages=stages,
            )
        if text_layer is not None and text_layer.lost_pages:
            lost_regions.append(text_layer.disclosure())

    try:
        parsed: ParsedDocument
        #: Pre-expansion budgets (T-V1-031): enforced while the parser expands
        #: the document, so a pathological input is refused between elements
        #: instead of after it has already cost the time and the memory.
        parsed, parse_report = await parse_with_precedence(
            content,
            mime_type=mime_type,
            filename=document.source_filename if document else None,
            metadata=parse_metadata,
            budgets=budgets_from_env(max_output_elements=max_elements()),
        )
        ensure_element_limit(parsed.elements)  # BEFORE chunk/DB writes
    except IngestPipelineError:
        raise
    except ParseBudgetExceeded as exc:
        logger.warning(
            "ingest_parse_budget_exceeded",
            extra={
                "event": "ingest_parse_budget_exceeded",
                "job_id": str(job.id),
                "budget": exc.code,
                "error": exc.message,
            },
        )
        raise IngestPipelineError(exc.code, exc.message, retryable=False) from exc
    except IngestionError as exc:
        raise IngestPipelineError(exc.code, exc.message, retryable=False) from exc
    except Exception as exc:  # unexpected/transient -> retryable
        raise IngestPipelineError(
            "PARSE_FAILED", f"{type(exc).__name__}: {exc}", retryable=True
        ) from exc

    # T-V1-019 — PRE-WRITE gate. Everything below writes publication state
    # (chunk rows, FTS rows, vector points) and finally READY, so a generation
    # already replaced or deleted is refused here, before a single write.
    await assert_generation_is_publishable(
        session,
        job=job,
        document_id=job.document_id,
        version_id=version.id,
        gate="write",
    )

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
            "CHUNK_FAILED", f"{type(exc).__name__}: {exc}", retryable=True
        ) from exc

    await session.execute(delete(ChunkRow).where(ChunkRow.document_version_id == version.id))
    #: T-V1-031: every chunk carries which parser read it and, when part of the
    #: source was unreadable, the lost regions — the answer layer reads this
    #: straight back off the retrieved items.
    chunk_disclosure = parse_disclosure(parse_report, lost_regions)
    id_map: dict[str, uuid.UUID] = {}
    for chunk in result.chunks:
        # T-V1-016: stable publication ID — same generation + same chunk ⇒ same
        # UUID, so a retried job overwrites its own rows/points instead of
        # minting a fresh random ID set (the old uuid4() accumulation bug).
        row_id = stable_chunk_id(version.id, chunk.id)
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
                chunk_metadata={
                    **(chunk.metadata or {}),
                    **chunk_disclosure,
                },
            )
        )
    if document is not None:
        # Document-level metadata (language/title) is written after the
        # T-V1-019 READY gate below, so a fenced generation makes no writes at
        # all. Supersession stays here: it is generation-scoped to *strictly
        # older* versions and can never republish this job's own generation.
        await supersede_other_versions(session, document.id, version.id)

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
            "FTS_INDEXING_FAILED",
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

            # Shape check only (TASK-Q1): one vector per chunk. Zero / NaN / Inf /
            # wrong-dimension vectors are rejected at the single storage boundary
            # (T-V1-015, qdrant_store.validate_records) — not re-checked per caller.
            if len(vectors) != len(texts):
                raise IngestPipelineError(
                    "EMBED_UNAVAILABLE",  # Q0 taxonomy code (packages/core/errors.py)
                    f"embedding returned {len(vectors)} vectors for {len(texts)} chunks",
                    retryable=True,
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
                    "QDRANT_INDEXING_FAILED",
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
            "QDRANT_INDEXING_FAILED",
            f"Embedding and vector store indexing failed: {exc}",
            retryable=True,
        ) from exc

    # TASK-Q1 verified-write gate: READY requires every mandatory store write
    # (Postgres FTS + Qdrant) to have been re-read and counted successfully.
    if not (fts_write_verified and vector_write_verified):
        raise IngestPipelineError(
            "STORE_WRITE_UNVERIFIED",  # Q0 taxonomy code (packages/core/errors.py)
            "refusing READY: mandatory store writes are unverified "
            f"(fts={fts_write_verified}, vectors={vector_write_verified})",
            retryable=False,
        )

    # T-V1-019 — PRE-READY gate. The document may have been replaced or
    # deleted while parse/chunk/index ran: this generation must then neither
    # publish (its writes are undone by the gate) nor be marked READY. The
    # gate's unpublish commits before it raises, so the worker's rollback
    # cannot silently restore the stale generation.
    await assert_generation_is_publishable(
        session,
        job=job,
        document_id=job.document_id,
        version_id=version.id,
        gate="ready",
    )

    # Only a generation that is still the document's live one may write
    # document-level metadata (moved here from the chunk stage so the gate
    # fences every write this pipeline makes, not only the store writes).
    if document is not None:
        document.language = result.language
        if document.title is None:
            document.title = result.metadata.title

    # Optional Graph Extraction & Indexing (V2 / RP-04 / QF-04). Runs after
    # the T-V1-019 gate for the same reason: a fenced generation must leave
    # no index rows behind, and the partial disclosure below only belongs on
    # a job that actually publishes.
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
                    "event": "graph_extraction_indexed",
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

    #: T-V1-031: content that was only partially read is NOT a clean ``ready``.
    #: The chunks (and the readable text) are published so retrieval works over
    #: what *was* read, but the job is parked ``needs_review`` with the lost
    #: regions named, and every chunk carries the same disclosure — so an answer
    #: over an omitted region can never present itself as complete.
    needs_review_reasons = needs_review_reasons_for(lost_regions)
    if needs_review_reasons:
        # Partial read: park needs_review. An earlier partial reason (e.g.
        # GRAPH_INDEX_PARTIAL) is overwritten here and only here — a document
        # read in full keeps the reasons it already had.
        job.status = JobStatus.NEEDS_REVIEW.value
        job.error_code = "NEEDS_REVIEW"
        job.error_message = ("NEEDS_REVIEW: " + "; ".join(needs_review_reasons))[:2000]
    else:
        # Full read: the T-V1-019 ready gate above is what authorises this
        # transition, so it must come before it (pinned by test_ingest_pipeline).
        job.status = JobStatus.READY.value
    job.stage = JobStage.READY.value
    job.finished_at = datetime.now(UTC)
    await session.commit()
    stages.append(JobStage.READY.value)
    _log_stage(job, JobStage.READY.value)

    logger.info(
        "ingest_job_ready" if not needs_review_reasons else "ingest_job_needs_review",
        extra={
            "event": "ingest_job_ready"
            if not needs_review_reasons
            else "ingest_job_needs_review",
            "job_id": str(job.id),
            "chunks": len(result.chunks),
            "language": result.language,
            "partial": bool(partial_reasons) or bool(lost_regions),
            "needs_review": bool(needs_review_reasons),
            "parse": parse_report.as_metadata(),
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
        partial=bool(partial_reasons) or bool(lost_regions),
        warnings=list(partial_reasons) + needs_review_reasons,
        needs_review=bool(needs_review_reasons),
        lost_regions=list(lost_regions),
    )
