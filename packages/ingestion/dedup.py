"""Checksum dedup, immutable versions, delete tombstones (M2-001d).

Postgres is fully handled here. Vector (Qdrant), full-text (FTS) and the
document originals themselves are purged behind the store interfaces; this
module exposes the registration seam and reports which stores were purged.

Since T-V1-039 the original bytes live in object storage
(``storage.originals``, keyed ``originals/{workspace}/{document}/{version}``)
and Postgres keeps only ``storage_key`` + ``checksum`` — so a purge that
tombstones rows and leaves the objects behind is a *logical* delete, not a
delete: the original stays retrievable. T-V1-055 deletes them here too, as the
``originals`` entry of the same per-store report.

T-V1-017: **supersession** (version replace) cleans the superseded version's
FTS rows and Qdrant points too, not only its Postgres chunk rows — otherwise a
replaced version stays retrievable. A failed store cleanup is queued for retry
(``SUPERSESSION_CLEANUP_*`` markers on the job), never silently dropped.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ingestion.persistence import ChunkRow, Document, DocumentVersion, IngestionJob

if TYPE_CHECKING:
    from retrieval.interfaces import SearchFilter

from core.config import vector_collection

logger = logging.getLogger("knowledge_os.ingestion.dedup")

StorePurger = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]

#: Store purgers registered by later adapters (M3-001a: qdrant/fts).
_STORE_PURGERS: dict[str, StorePurger] = {}


def build_purge_filter(workspace_id: uuid.UUID, document_id: uuid.UUID) -> SearchFilter:
    """Q3 tombstone filter: scope a purge to one document in one workspace.

    Factored out so the filter shape (``document_ids=[str(document_id)]`` and
    ``workspace_ids=[str(workspace_id)]``) is unit-testable without live
    Qdrant/Postgres; both default store purgers must use this exact filter.
    """
    from retrieval.interfaces import SearchFilter

    return SearchFilter(
        workspace_ids=[str(workspace_id)],
        document_ids=[str(document_id)],
    )


def register_store_purger(name: str, purger: StorePurger) -> None:
    """Register a downstream store purge (M3 seam)."""
    _STORE_PURGERS[name] = purger


def registered_stores() -> tuple[str, ...]:
    return tuple(sorted(_STORE_PURGERS))


def originals_prefix(workspace_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """The prefix that holds exactly one document's originals (T-V1-055).

    ``storage.originals`` keys every original ``originals/{workspace}/
    {document}/{version}…``, so this prefix is this document's objects and
    nobody else's — the property that makes a prefix sweep safe, and what
    catches an original whose key Postgres never recorded (a version row that
    predates ``storage_key``).
    """
    from storage.originals import ORIGINALS_PREFIX

    return f"{ORIGINALS_PREFIX}/{workspace_id}/{document_id}/"


async def delete_document_originals(
    *,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    storage_keys: Sequence[str],
) -> int:
    """Delete a document's originals from object storage; return how many went.

    T-V1-055, closing T-039's risk 1: a purge that leaves the bytes in the
    store is a *logical* delete, not a delete — after it the original is still
    retrievable, which is a privacy/retention hole rather than a latency one.

    Two passes, because neither list alone is complete:

    * every ``storage_key`` the version rows record — exact, and complete no
      matter how many versions the document has;
    * a sweep of :func:`originals_prefix` — the safety net for a legacy version
      row written before ``storage_key`` existed (its object can only be found
      by listing, since the key's extension came from a filename no longer
      recorded anywhere) and for any object under a key Postgres no longer
      tracks.

    An absent object is **not** a failure: an original that was never written
    (the ``EMPTY_DOCUMENT`` row without a key) has nothing to delete and the
    adapter simply reports ``False``. A raised :class:`~storage.interfaces.StorageError`
    is the only way this function says "the bytes may still be there", and the
    caller reports it per-store like any other store failure — the message
    never reaches a user, only the per-store report and the log.
    """
    from storage.originals import get_adapter, originals_bucket

    store = get_adapter()
    bucket = originals_bucket()
    targets = [key for key in dict.fromkeys(storage_keys) if key]
    listed = await store.list_objects(
        bucket, prefix=originals_prefix(workspace_id, document_id)
    )
    targets.extend(obj.key for obj in listed if obj.key not in targets)
    removed = 0
    for key in targets:
        if await store.delete_object(bucket, key):
            removed += 1
    return removed


async def find_document_by_filename(
    session: AsyncSession, workspace_id: uuid.UUID, filename: str | None
) -> Document | None:
    """Find a live document by (workspace, source filename) for dedup/versioning."""
    if not filename:
        return None
    statement = (
        select(Document)
        .where(
            Document.workspace_id == workspace_id,
            Document.source_filename == filename,
            Document.deleted_at.is_(None),
        )
        .order_by(Document.created_at.desc())
    )
    return (await session.execute(statement)).scalars().first()


async def latest_version(session: AsyncSession, document_id: uuid.UUID) -> DocumentVersion | None:
    statement = (
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_number.desc())
    )
    return (await session.execute(statement)).scalars().first()


async def latest_job_for_version(
    session: AsyncSession, version_id: uuid.UUID
) -> IngestionJob | None:
    statement = (
        select(IngestionJob)
        .where(IngestionJob.document_version_id == version_id)
        .order_by(IngestionJob.created_at.desc())
    )
    return (await session.execute(statement)).scalars().first()


async def supersede_other_versions(
    session: AsyncSession, document_id: uuid.UUID, keep_version_id: uuid.UUID
) -> int:
    """Supersede **strictly older** versions only (never clobber a newer one).

    A retryable failure can park an older version's job while a newer version
    completes first; when the older retry finishes, it must not delete the newer
    version's chunks.

    T-V1-017: supersession now also removes the older versions' **FTS rows**
    (same transaction — atomic with the chunk delete) and **Qdrant points**
    (store cleanup call). A Qdrant cleanup failure does not fail the ingest
    job (the *new* version is healthy): it is queued for retry and flagged on
    the job so stale points are visibly pending, never silently kept.
    """
    keep = await session.get(DocumentVersion, keep_version_id)
    if keep is None:
        return 0
    older = (
        (
            await session.execute(
                select(DocumentVersion.id).where(
                    DocumentVersion.document_id == document_id,
                    DocumentVersion.version_number < keep.version_number,
                )
            )
        )
        .scalars()
        .all()
    )
    if not older:
        return 0
    # T-V1-017 (1): the superseded version's lexical rows leave in the SAME
    # transaction as its chunk rows, so Postgres can never hold a half-superseded
    # version (SQL here is implementation, not evidence — the test asserts via
    # live search + citation hydration).
    await session.execute(
        text("DELETE FROM lexical_docs WHERE document_version_id = ANY(:ids)"),
        {"ids": [str(v) for v in older]},
    )
    await session.execute(delete(ChunkRow).where(ChunkRow.document_version_id.in_(older)))
    await session.execute(
        update(DocumentVersion)
        .where(DocumentVersion.id.in_(older))
        .values(superseded_at=datetime.now(UTC))
    )
    # T-V1-017 (2): Qdrant points for the superseded versions, via the store's
    # version-scoped cleanup call (payload document_version_id).
    await _cleanup_superseded_vectors(
        session, document_id=document_id, version_ids=older, keep_version_id=keep_version_id
    )
    return len(older)


async def _cleanup_superseded_vectors(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    version_ids: Sequence[uuid.UUID],
    keep_version_id: uuid.UUID,
) -> bool:
    """Remove superseded versions' Qdrant points; queue a retry when it fails.

    Failure is never silent: the job is marked ``SUPERSESSION_CLEANUP_RETRY``
    (cleared by the next run's ``error_code = None``) and the job id is put on
    the delayed queue so the worker re-runs it and retries this cleanup. Once
    ``attempts >= max_attempts`` the marker becomes ``SUPERSESSION_CLEANUP_FAILED``
    and the failure stays loud in the logs.
    """
    ids = [str(v) for v in version_ids]
    try:
        from retrieval.adapters.qdrant_store import QdrantVectorStore

        collection = vector_collection()
        qdrant = QdrantVectorStore(collection=collection)
        try:
            removed = await qdrant.delete_versions(
                collection=collection,
                version_ids=ids,
                document_id=str(document_id),
            )
        finally:
            await qdrant.close()
        logger.info(
            "supersession_vectors_cleaned",
            extra={
                "event": "supersession_vectors_cleaned",
                "document_id": str(document_id),
                "superseded_version_ids": ids,
                "points_removed": removed,
            },
        )
        return True
    except Exception as exc:
        job = await latest_job_for_version(session, keep_version_id)
        detail = f"superseded-version Qdrant cleanup failed: {type(exc).__name__}: {exc}"[:2000]
        if job is None:
            logger.warning(
                "supersession_cleanup_failed",
                extra={
                    "event": "supersession_cleanup_failed",
                    "document_id": str(document_id),
                    "superseded_version_ids": ids,
                    "job_id": None,
                    "retry_queued": False,
                    "error": str(exc),
                },
            )
            return False
        will_retry = job.attempts < job.max_attempts
        job.error_code = (
            "SUPERSESSION_CLEANUP_RETRY" if will_retry else "SUPERSESSION_CLEANUP_FAILED"
        )
        job.error_message = detail
        logger.warning(
            "supersession_cleanup_failed",
            extra={
                "event": "supersession_cleanup_failed",
                "document_id": str(document_id),
                "superseded_version_ids": ids,
                "job_id": str(job.id),
                "retry_queued": will_retry,
                "error": str(exc),
            },
        )
        if will_retry:
            await _queue_cleanup_retry(job)
        return False


async def _queue_cleanup_retry(job: IngestionJob) -> None:
    """Put the job back on the delayed queue so the cleanup is retried (T-V1-017)."""
    from ingestion.queue import IngestQueueUnavailable, schedule_retry

    try:
        await schedule_retry(str(job.id), 2.0 ** min(job.attempts, 5))
        logger.warning(
            "supersession_cleanup_retry_queued",
            extra={
                "event": "supersession_cleanup_retry_queued",
                "job_id": str(job.id),
                "attempts": job.attempts,
                "max_attempts": job.max_attempts,
            },
        )
    except IngestQueueUnavailable as exc:
        # No retry path available: keep the FAILED-style marker so the orphan
        # points are an acknowledged, greppable problem — never a silent one.
        job.error_code = "SUPERSESSION_CLEANUP_FAILED"
        logger.error(
            "supersession_cleanup_retry_unavailable",
            extra={
                "event": "supersession_cleanup_retry_unavailable",
                "job_id": str(job.id),
                "error": str(exc),
            },
        )


async def purge_document(
    session: AsyncSession, *, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> dict[str, bool]:
    """Tombstone a document and remove its rows; returns a per-store report.

    T-V1-055: a purged document's **originals** leave the object store too
    (T-039's open hole — the tombstone alone left the bytes retrievable). It is
    registered as the ``originals`` store purger, so it lands in the same
    per-store report and the same failure path as fts/qdrant/graph: a failed
    delete is ``"originals": False``, which the caller surfaces as 207 — never
    a false 204.

    Ordering is deliberate: the object delete runs *after* the tombstone
    commits, so a commit that fails can never destroy bytes for a document that
    is still live. The failure mode stays "tombstoned, bytes kept" instead of
    becoming "live document, bytes gone".
    """
    document = await session.get(Document, document_id)
    if document is None or document.workspace_id != workspace_id:
        return {}

    version_ids = (
        (
            await session.execute(
                select(DocumentVersion.id).where(DocumentVersion.document_id == document_id)
            )
        )
        .scalars()
        .all()
    )
    # T-V1-055: which objects this document's originals live under, read through
    # the same session this module already owns (and in the same transaction as
    # the chunk rows about to go), so the delete can never be driven by a key
    # list that disagrees with what was just tombstoned.
    recorded_keys = (
        (
            await session.execute(
                select(DocumentVersion.storage_key).where(
                    DocumentVersion.document_id == document_id,
                    DocumentVersion.storage_key.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    original_keys = [key for key in recorded_keys if key]
    if version_ids:
        await session.execute(delete(ChunkRow).where(ChunkRow.document_version_id.in_(version_ids)))
    document.deleted_at = datetime.now(UTC)
    await session.commit()

    report: dict[str, bool] = {"postgres": True}

    if not _STORE_PURGERS:
        # Default lazy registration of real store purgers (TASK-Q3 / M3-001a)
        try:
            from retrieval.adapters.pg_fts import PostgresLexicalStore
            from retrieval.adapters.qdrant_store import QdrantVectorStore

            async def _purge_fts(ws_id: uuid.UUID, doc_id: uuid.UUID) -> None:
                store = PostgresLexicalStore()
                filt = build_purge_filter(ws_id, doc_id)
                await store.delete(query_filter=filt)

            async def _purge_qdrant(ws_id: uuid.UUID, doc_id: uuid.UUID) -> None:
                collection = vector_collection()
                qdrant = QdrantVectorStore(collection=collection)
                filt = build_purge_filter(ws_id, doc_id)
                await qdrant.delete(collection=collection, query_filter=filt)
                await qdrant.close()

            register_store_purger("fts", _purge_fts)
            register_store_purger("qdrant", _purge_qdrant)
        except Exception as exc:
            # Registration failure must never read as a clean purge: the
            # vector/lexical stores were NOT purged, so mark them failed in
            # the per-store report (review1 F2 — no false 204).
            logger.warning(
                "store_purger_registration_failed",
                extra={
                    "event": "store_purger_registration_failed",
                    "workspace_id": str(workspace_id),
                    "document_id": str(document_id),
                    "error": str(exc),
                },
            )
            report.setdefault("fts", False)
            report.setdefault("qdrant", False)

    # T-V1-007 (RP-04-lite, QF-04): the GRAPH_MODE check is re-evaluated on
    # EVERY call, not only when the registry is empty, so a late
    # ``GRAPH_MODE=rule`` toggle still purges graph rows on the next delete
    # (the registry is non-empty from the first purge onwards, which used to
    # freeze the graph decision made at that moment). Registration is keyed
    # by name, so re-registering is idempotent — exactly one purge per store
    # per call, never a double-purge. Toggling the mode back off unregisters
    # the purger again so a stale closure never fires.
    try:
        if os.environ.get("GRAPH_MODE", "off").strip().lower() in ("rule", "llm"):
            from graph.factory import graph_db_path
            from graph.sqlite_store import SQLiteGraphStore

            async def _purge_graph(ws_id: uuid.UUID, doc_id: uuid.UUID) -> None:
                gstore = SQLiteGraphStore(graph_db_path())
                await gstore.tombstone_document(str(ws_id), str(doc_id))

            register_store_purger("graph", _purge_graph)
        else:
            _STORE_PURGERS.pop("graph", None)
    except Exception as exc:
        # Same no-false-204 rule as the fts/qdrant seam: a graph purge that
        # cannot even be registered is reported as a failed store, never
        # silently dropped from the per-store report.
        logger.warning(
            "graph_purger_registration_failed",
            extra={
                "event": "graph_purger_registration_failed",
                "workspace_id": str(workspace_id),
                "document_id": str(document_id),
                "error": str(exc),
            },
        )
        report.setdefault("graph", False)

    # T-V1-055 (T-039 risk 1) + T-V1-058: the originals leave the store too.
    # Registration **order is load-bearing**: this block must come AFTER the
    # default fts/qdrant block above, because that block is guarded by
    # ``if not _STORE_PURGERS`` — registering any purger before it leaves the
    # registry non-empty and the default stores are then never registered, so
    # a purge would report ``{"postgres": True, "originals": True}`` (a clean
    # 204) while its lexical and vector rows survive. That is exactly the
    # regression T-V1-058 caught, one line above the comment you are reading.
    #
    # Otherwise as before: registered fresh on every call with the keys *this*
    # call read — never a stale closure left over from an earlier document —
    # and unregistered again when there is nothing to delete, exactly like the
    # GRAPH_MODE toggle above. A purge that cannot delete its objects is
    # reported per-store by the same loop below that already reports
    # fts/qdrant/graph, so no new failure shape is invented.
    if version_ids:
        async def _purge_originals(ws_id: uuid.UUID, doc_id: uuid.UUID) -> None:
            removed = await delete_document_originals(
                workspace_id=ws_id,
                document_id=doc_id,
                storage_keys=original_keys,
            )
            logger.info(
                "document_originals_purged",
                extra={
                    "event": "document_originals_purged",
                    "workspace_id": str(ws_id),
                    "document_id": str(doc_id),
                    "objects_removed": removed,
                    "storage_keys": len(original_keys),
                },
            )

        register_store_purger("originals", _purge_originals)
    else:
        # No version rows: nothing in the store can belong to this document, and
        # a purger left registered by an earlier purge must not fire here.
        _STORE_PURGERS.pop("originals", None)

    for name, purger in _STORE_PURGERS.items():
        try:
            await purger(workspace_id, document_id)
            report[name] = True
        except Exception as exc:
            logger.warning(
                "store_purge_failed",
                extra={
                    "event": "store_purge_failed",
                    "store": name,
                    "workspace_id": str(workspace_id),
                    "document_id": str(document_id),
                    "error": str(exc),
                },
            )
            report[name] = False
    return report
