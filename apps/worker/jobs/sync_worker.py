"""Sync worker — durable production wiring for queued connector sync jobs (M6-001b).

Production defaults, not memory helpers:

- the DURABLE progress store (:class:`connectors.adapters.progress_store.
  SqlAlchemySyncProgressStore` — one shared metadata DB, the same session factory
  ingestion owns), so a restarted worker resumes a source's generation/cursor;
- the Redis sync queue (:class:`connectors.adapters.sync_queue.RedisSyncQueue`);
- a real document PUBLISHER (new/changed items are ingested as workspace documents
  through the same semantics as the upload route: originals to object storage BEFORE
  the row commits, a new ``DocumentVersion`` + queued ``IngestionJob``, then enqueue);
- a real document DELETER (an item absent from a completed full run tombstones its
  document through ``purge_document`` — the same call the delete route makes).

Every seam stays injectable (store/queue/publisher/deleter + their inner seams), so
tests drive the same code paths and services can be faked where unavailable. The
worker is authenticated by its connector identity: the ``SourceIdentity`` carries the
workspace, and the worker only ever writes inside it — no principal bypass, no
cross-workspace writes (product ACL preserved).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import mimetypes
import os
import uuid
from typing import Any

from connectors.adapters.progress_store import SqlAlchemySyncProgressStore
from connectors.adapters.sync_queue import RedisSyncQueue
from connectors.interfaces import SourceItem
from connectors.sync_consumer import (
    QueuedSyncConsumer,
    SyncProgressStore,
)

logger = logging.getLogger("knowledge_os.worker.sync")


class SyncPurgeError(RuntimeError):
    """A document's store purge was incomplete (root review): fail closed.

    Raised by the production deleter when ``purge_document`` reports any store
    still holding derived rows. The mapping is deliberately kept so the
    redelivery can retry the purge and converge.
    """

#: Env flag gating the sync loop in worker startup. Off (the default) starts
#: nothing and keeps the flag-off path byte-identical to before — the same
#: contract GRAPH_MODE uses for the graph consumer.
SYNC_WORKER_ENV = "SYNC_WORKER_ENABLED"

#: Supervisor policy for the sync loop (restart with growing backoff).
DEFAULT_RESTART_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 30.0
DEFAULT_MAX_CONSECUTIVE_FAILURES = 10


def sync_worker_enabled() -> bool:
    """True when the operator enabled the sync loop in worker startup."""
    return os.environ.get(SYNC_WORKER_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def default_source_loader() -> Any:
    """Read persisted source settings through the package present in worker images."""
    from connectors.adapters.config_loader import load_persisted_sources

    return load_persisted_sources


# ---------------------------------------------------------------------------
# Production document wiring (the real ingest/tombstone paths)
# ---------------------------------------------------------------------------


def _open_session():
    """One session from the durable ingestion metadata DB (shared engine)."""
    from ingestion.persistence import get_session_factory

    return get_session_factory()()


def _default_put_original(**kwargs: Any) -> Any:
    from storage.originals import put_original

    return put_original(**kwargs)


def _default_enqueue(job_id: str) -> Any:
    from ingestion.queue import enqueue

    return enqueue(job_id)


def _default_recover_expired() -> Any:
    """The production lease recovery (T-V1-020): redeliver expired sync claims."""
    from connectors.adapters.sync_queue import recover_expired

    return recover_expired()


async def _repair_dispatch(
    session: Any,
    *,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    enqueue_fn: Any,
) -> None:
    """Guarantee a committed version has a dispatched ingestion job.

    The lost-ingestion case POLICE found: the version+job transaction commits,
    then the enqueue fails. The redelivered retry sees the SAME checksum, and a
    naive same-checksum early-return never re-dispatches — the document sits
    `queued` forever. So the same-checksum path does not return early: it
    verifies the dispatch and repairs it (enqueue is idempotent by job id, so a
    duplicate dispatch is harmless while a missing one is fatal).
    """
    from ingestion.dedup import latest_job_for_version
    from ingestion.persistence import IngestionJob

    latest_job = await latest_job_for_version(session, version_id)
    if latest_job is not None and latest_job.status in {"queued", "running"}:
        # In-flight: (re)dispatch — idempotent by job id.
        await enqueue_fn(str(latest_job.id))
        return
    if latest_job is not None and latest_job.status == "ready":
        return  # ingested; nothing to repair
    # No job, or a failed/unknown one: re-parse the same version (the upload
    # route's failed-attempt semantics).
    job = IngestionJob(
        workspace_id=workspace_id,
        document_id=document_id,
        document_version_id=version_id,
        status="queued",
        stage="queued",
    )
    session.add(job)
    await session.commit()
    await enqueue_fn(str(job.id))


def make_document_publisher(
    *,
    session_factory: Any = None,
    put_original_fn: Any = None,
    enqueue_fn: Any = None,
) -> Any:
    """Build the production ``Publisher``: ingest an item as a workspace document.

    Identity comes from the PERSISTED MAPPING (workspace, connector,
    external_id) -> document_id — never from a filename guess: nested external
    ids keep their own document, and two connectors with the same basename
    never overwrite each other. Semantics otherwise mirror the upload route
    (T-V1-039 / PD-005): originals are stored BEFORE the row commits, then a
    new version + queued job + the mapping row commit atomically, then enqueue.
    """
    from connectors.adapters.source_document_map import (
        get_mapped_document,
        put_mapping,
    )
    from ingestion.dedup import latest_version
    from ingestion.persistence import Document, DocumentVersion, IngestionJob

    session_factory = session_factory or _open_session
    put_original_fn = put_original_fn or _default_put_original
    enqueue_fn = enqueue_fn or _default_enqueue

    async def publisher(identity: Any, item: SourceItem, content: bytes, checksum: str) -> None:
        workspace_id = uuid.UUID(identity.workspace_id)
        connector_id = identity.connector_id
        external_id = item.external_id
        filename = (item.name or external_id)[:512]
        mime_type = (item.mime_type or mimetypes.guess_type(filename)[0]
                     or "application/octet-stream").split(";", 1)[0].strip()
        async with session_factory() as session:
            mapped = await get_mapped_document(
                session,
                workspace_id=workspace_id,
                connector_id=connector_id,
                external_id=external_id,
            )
            document = await session.get(Document, mapped) if mapped is not None else None
            if document is not None and document.deleted_at is not None:
                # TOMBSTONED mapped document (purged outside sync): the same
                # checksum must NOT silently preserve the deleted state — a
                # reappeared item resurrects into a NEW document and the
                # mapping is repointed (root review). Dropping the soft-delete
                # here would re-advertise content the delete route removed.
                document = None
                mapped = None
            if mapped is not None and document is None:
                # The mapped document vanished without a tombstone: the stale
                # row must not wedge the source — replace it below.
                mapped = None
            if document is not None:
                latest = await latest_version(session, document.id)
                if latest is not None and latest.checksum == checksum:
                    # Same content — but NEVER a silent early return: verify
                    # the dispatch is alive (repairs the lost-enqueue case).
                    await _repair_dispatch(
                        session,
                        workspace_id=workspace_id,
                        document_id=document.id,
                        version_id=latest.id,
                        enqueue_fn=enqueue_fn,
                    )
                    return
                version_number = (latest.version_number if latest else 0) + 1
            else:
                document = Document(
                    workspace_id=workspace_id,
                    title=filename,
                    source_filename=filename,
                    mime_type=mime_type,
                )
                session.add(document)
                await session.flush()
                version_number = 1

            version_id = uuid.uuid4()
            try:
                storage_key = await put_original_fn(
                    workspace_id=workspace_id,
                    document_id=document.id,
                    version_id=version_id,
                    data=content,
                    checksum=checksum,
                    filename=filename,
                )
            except Exception:
                # Object not stored: refuse the rows (a document whose original
                # is missing can never be parsed again). The consumer records
                # FAILED and does not ack, so the item is retried.
                await session.rollback()
                raise

            session.add(
                DocumentVersion(
                    id=version_id,
                    document_id=document.id,
                    version_number=version_number,
                    checksum=checksum,
                    size_bytes=len(content),
                    mime_type=mime_type,
                    storage_key=storage_key,
                )
            )
            job = IngestionJob(
                workspace_id=workspace_id,
                document_id=document.id,
                document_version_id=version_id,
                status="queued",
                stage="queued",
            )
            session.add(job)
            await put_mapping(
                session,
                workspace_id=workspace_id,
                connector_id=connector_id,
                external_id=external_id,
                document_id=document.id,
                # Fresh, or the stale-row repoint after an external purge
                # (mapped was reset to None for a document that no longer
                # exists). An existing row with a LIVE document is untouched.
                replace=mapped is None,
            )
            await session.commit()
            await enqueue_fn(str(job.id))

    return publisher


def make_document_deleter(
    *,
    session_factory: Any = None,
    purge_fn: Any = None,
) -> Any:
    """Build the production ``Deleter``: tombstone a deleted item's document.

    Identity comes from the PERSISTED MAPPING — the full external id, so a
    nested item deletes exactly its document and a shared basename across
    connectors cannot delete the wrong one. Semantics otherwise mirror the
    delete route: ``purge_document`` (tombstone + every derived store through
    the registered purgers), then the mapping row drops in the same session.
    An unmapped item was never ingested here — nothing to purge; the
    consumer's DELETED outcome is the record either way.
    """
    from connectors.adapters.source_document_map import (
        delete_mapping,
        get_mapped_document,
    )
    from ingestion.dedup import purge_document

    session_factory = session_factory or _open_session
    purge_fn = purge_fn or purge_document

    async def deleter(identity: Any, external_id: str, prior: Any) -> None:
        workspace_id = uuid.UUID(identity.workspace_id)
        connector_id = identity.connector_id
        async with session_factory() as session:
            document_id = await get_mapped_document(
                session,
                workspace_id=workspace_id,
                connector_id=connector_id,
                external_id=external_id,
            )
            if document_id is None:
                return  # never ingested here — nothing to tombstone
            report = await purge_fn(session, workspace_id=workspace_id, document_id=document_id)
            failed = sorted(name for name, ok in (report or {}).items() if not ok)
            if failed:
                # FAIL CLOSED (root review): a store that still holds derived
                # rows (vector/FTS/originals) means the tombstone is not
                # complete. Keep the mapping and RAISE so the consumer records
                # FAILED and does not ack — the lease expires and the
                # redelivery retries the purge (idempotent: the tombstone is
                # already set, the failed stores are retried). Dropping the
                # mapping here is what orphaned the item forever.
                raise SyncPurgeError(
                    f"document {document_id} purge incomplete: stores failed {failed}"
                )
            await delete_mapping(
                session,
                workspace_id=workspace_id,
                connector_id=connector_id,
                external_id=external_id,
            )
            await session.commit()

    return deleter


# ---------------------------------------------------------------------------
# Worker entrypoints
# ---------------------------------------------------------------------------


def default_sync_consumer(
    *,
    store: SyncProgressStore | None = None,
    queue: Any = None,
    publisher: Any = None,
    deleter: Any = None,
    load_sources: Any = None,
    connector_factory: Any = None,
    page_size: int = 100,
    max_pages: int = 10_000,
) -> QueuedSyncConsumer:
    """The production consumer: durable store, Redis queue, real wiring."""
    return QueuedSyncConsumer(
        store=store if store is not None else SqlAlchemySyncProgressStore(),
        queue=queue if queue is not None else RedisSyncQueue(),
        publisher=publisher if publisher is not None else make_document_publisher(),
        deleter=deleter if deleter is not None else make_document_deleter(),
        load_sources=load_sources if load_sources is not None else default_source_loader(),
        connector_factory=connector_factory,
        page_size=page_size,
        max_pages=max_pages,
    )


async def run_sync_once(
    *,
    store: SyncProgressStore | None = None,
    queue: Any = None,
    publisher: Any = None,
    deleter: Any = None,
    load_sources: Any = None,
    connector_factory: Any = None,
    timeout_seconds: int = 1,
    page_size: int = 100,
    max_pages: int = 10_000,
) -> dict[str, Any]:
    """Claim and process one sync job, returning a JSON-safe summary dict.

    Defaults are the DURABLE production wiring. ``store=None`` used to mean an
    in-memory store — progress lost on every restart, which is what POLICE's
    M6-001b REQUEST-CHANGES closed. Tests inject fakes.
    """
    consumer = default_sync_consumer(
        store=store,
        queue=queue,
        publisher=publisher,
        deleter=deleter,
        load_sources=load_sources,
        connector_factory=connector_factory,
        page_size=page_size,
        max_pages=max_pages,
    )
    summary = await consumer.run_once(timeout_seconds=timeout_seconds)
    if summary is None:
        return {"claimed": False}
    return {
        "claimed": True,
        "job_id": summary.job_id,
        "workspace_id": summary.workspace_id,
        "connector_id": summary.connector_id,
        "acked": summary.acked,
        "reports": [dataclasses.asdict(r) for r in summary.reports],
    }


async def run_sync_loop(
    *,
    store: SyncProgressStore | None = None,
    queue: Any = None,
    publisher: Any = None,
    deleter: Any = None,
    load_sources: Any = None,
    connector_factory: Any = None,
    consumer: Any = None,
    recover_fn: Any = None,
    recover_interval_seconds: float = 30.0,
    idle_sleep_seconds: float = 1.0,
    max_iterations: int | None = None,
    stop_event: asyncio.Event | None = None,
) -> dict[str, Any]:
    """Continuously claim and process sync jobs until stopped/cancelled.

    The deleter IS threaded through here (POLICE finding: the loop previously
    never supplied one, so deletions were recorded but never tombstoned).
    ``consumer`` is an injection seam (mirrors the graph supervisor's own):
    tests drive a pre-built consumer; production builds the real one.

    Lease recovery (root runtime gap): every ``recover_interval_seconds`` the
    loop returns EXPIRED sync claims to the ready queue — a consumer that died
    mid-run leaves its claim un-acked on purpose, and without this a crashed
    job would sit claimed forever (no redelivery). ``recover_fn`` is the
    injection seam (tests count calls / use an isolated Redis namespace).

    Exception policy (root review, deliberate): a per-ITERATION failure is
    TRANSIENT — logged, backoff-slept, and the loop CONTINUES (a Redis blip or
    one poisoned job must not take the worker down; the claim's lease then
    expires and recovery redelivers). The SUPERVISOR covers loop-LEVEL
    failures (a bug in the loop itself, a broken construction) — tested
    separately. CancelledError (shutdown) propagates untouched.
    """
    if consumer is None:
        consumer = default_sync_consumer(
            store=store,
            queue=queue,
            publisher=publisher,
            deleter=deleter,
            load_sources=load_sources,
            connector_factory=connector_factory,
        )
    recover_fn = recover_fn or _default_recover_expired
    runs = 0
    processed = 0
    recoveries = 0
    last_recovery = 0.0
    iterations = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        now = asyncio.get_running_loop().time()
        if now - last_recovery >= recover_interval_seconds:
            try:
                moved = await recover_fn()
                recoveries += 1
                if moved:
                    logger.info(
                        "sync_leases_recovered",
                        extra={"event": "sync_leases_recovered", "moved": moved},
                    )
            except Exception:
                # A recovery failure must not kill the loop either — the next
                # tick retries it; claims simply stay claimed a while longer.
                logger.warning("sync_lease_recovery_failed")
            last_recovery = now
        try:
            summary = await consumer.run_once(timeout_seconds=1)
            if summary is not None:
                runs += 1
                processed += len(summary.reports)
                logger.info(
                    "sync_job_processed",
                    extra={
                        "event": "sync_job_processed",
                        "job_id": summary.job_id,
                        "workspace_id": summary.workspace_id,
                        "connector_id": summary.connector_id,
                        "items": len(summary.reports),
                        "acked": summary.acked,
                    },
                )
            else:
                await asyncio.sleep(idle_sleep_seconds)
        except Exception:  # pragma: no cover - defensive loop guard
            logger.warning("sync_worker_iteration_failed")
            await asyncio.sleep(idle_sleep_seconds)
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            break
    return {
        "iterations": iterations,
        "runs": runs,
        "items_processed": processed,
        "recoveries": recoveries,
    }


async def run_supervised_sync_consumer(
    stop_event: asyncio.Event,
    *,
    loop_fn: Any = None,
    backoff: float = DEFAULT_RESTART_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_MAX_BACKOFF_SECONDS,
    max_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    **consumer_kwargs: Any,
) -> dict[str, Any]:
    """Run the sync loop under a restart policy until ``stop_event``.

    Mirrors the graph/agent supervision contract: a crashing LOOP (a bug in
    the loop itself — NOT a per-iteration transient, which the loop handles
    by design) is restarted with growing backoff instead of vanishing without
    a trace; too many consecutive failures give up loudly (the operator sees
    the error). ``loop_fn`` is the injection seam for that loop-level path
    (tests inject a failing loop to prove the restart policy fires).
    """
    failures = 0
    restarts = 0
    loop_fn = loop_fn or run_sync_loop
    while not stop_event.is_set():
        try:
            stats = await loop_fn(stop_event=stop_event, **consumer_kwargs)
            return {"restarts": restarts, "consecutive_failures": failures, **stats}
        except Exception as exc:
            failures += 1
            restarts += 1
            logger.warning(
                "sync_consumer_crashed",
                extra={"event": "sync_consumer_crashed", "restart": restarts},
            )
            if failures >= max_failures:
                logger.error(
                    "sync_consumer_giving_up",
                    extra={"event": "sync_consumer_giving_up", "failures": failures},
                )
                return {"restarts": restarts, "consecutive_failures": failures,
                        "error": "sync_consumer_unavailable", "error_type": type(exc).__name__}
            await asyncio.sleep(min(backoff * failures, max_backoff))
    return {"restarts": restarts, "consecutive_failures": failures}
