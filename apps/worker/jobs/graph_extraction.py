"""Queued graph extraction (T-V2-001).

Runs as a background job behind GRAPH_MODE=rule|llm, never inline in the
ingest pipeline. Uses the T-V1-020 durable claim/ack discipline via
packages/graph/queue.py, with visible park codes and retry-not-resurrect.

Failure parks LOUDLY: a graph extraction failure is recorded as a terminal
FAILED state with a graph error code (GRAPH_EXTRACTION_FAILED), retried up to
max_attempts with backoff, never silent READY with missing graph rows.

Idempotency: RuleBasedGraphExtractor is deterministic and SQLiteGraphStore
upserts are idempotent (INSERT ... ON CONFLICT DO UPDATE), so retried
extractions do not duplicate entities/relationships.

Flag-off parity: when GRAPH_MODE=off, ingest_pipeline does a single env check
and never enqueues; no graph DB is touched, no queue operation, ~zero overhead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

from graph.extractor import GraphChunk, RuleBasedGraphExtractor
from graph.factory import graph_db_path, graph_mode
from graph.queue import (
    Claim,
    GraphQueueUnavailable,
    claim,
    delete_payload,
    get_payload,
    promote_due,
    recover_expired,
    renew,
    schedule_retry,
)
from graph.sqlite_store import SQLiteGraphStore
from ingestion.persistence import ChunkRow, get_session_factory
from sqlalchemy import select

logger = logging.getLogger("knowledge_os.worker.graph")

_BACKOFF_CAP_SECONDS = 30
_DEFAULT_MAX_ATTEMPTS = 3
_GRAPH_ERROR_CODE = "GRAPH_EXTRACTION_FAILED"


def _is_graph_enabled() -> bool:
    return graph_mode() in ("rule", "llm")


async def enqueue_graph_extraction(
    workspace_id: str,
    document_id: str,
    version_id: str,
    chunk_ids: list[str] | None = None,
) -> str | None:
    """Enqueue a graph extraction job iff graph is enabled.

    Returns job id when enqueued, None when flag-off (no-op).
    This is the ONLY call site from ingest_pipeline - one env check, no DB,
    no queue operation when off (flag-off parity ~zero overhead).
    """
    if not _is_graph_enabled():
        return None
    from graph.queue import enqueue_graph_job

    try:
        job_id = await enqueue_graph_job(
            workspace_id=workspace_id,
            document_id=document_id,
            version_id=version_id,
            chunk_ids=chunk_ids,
        )
        logger.info(
            "graph_job_enqueued",
            extra={
                "event": "graph_job_enqueued",
                "job_id": job_id,
                "workspace_id": workspace_id,
                "document_id": document_id,
                "version_id": version_id,
            },
        )
        return job_id
    except Exception as exc:
        logger.warning(
            "graph_enqueue_failed",
            extra={
                "event": "graph_enqueue_failed",
                "workspace_id": workspace_id,
                "document_id": document_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        # T-R10: PROPAGATE. This used to `return None`, which is the same value
        # the graph-disabled skip returns — so a queue outage was
        # indistinguishable from "graph is off", the caller logged
        # graph_job_enqueued, and the document went READY with no graph job
        # anything would ever claim. A clean skip and a loud failure must not
        # share a return value. `None` now means exactly one thing: graph is
        # disabled and nothing was supposed to happen.
        raise


async def process_graph_job(job_id: str) -> dict:
    """Process one graph extraction job by job_id.

    Loads payload, fetches chunks for the version, runs extraction, upserts.
    Returns summary dict. Raises on failure for retry/park handling.
    Idempotent: re-running with same chunks yields same entities (deterministic
    node IDs) and upserts overwrite, no duplicates.
    """
    payload = await get_payload(job_id)
    if payload is None:
        raise RuntimeError(f"graph job payload not found for {job_id}")

    workspace_id = payload["workspace_id"]
    document_id = payload["document_id"]
    version_id = payload["version_id"]

    # Load chunks for this version from Postgres
    async with get_session_factory()() as session:
        db_result = await session.execute(
            select(ChunkRow).where(ChunkRow.document_version_id == uuid.UUID(version_id))
        )
        chunk_rows = db_result.scalars().all()

    if not chunk_rows:
        logger.info(
            "graph_no_chunks",
            extra={
                "event": "graph_no_chunks",
                "job_id": job_id,
                "version_id": version_id,
            },
        )
        return {"entities": 0, "relationships": 0, "communities": 0}

    graph_chunks = [
        GraphChunk(
            chunk_id=str(row.id),
            text=row.text,
            workspace_id=workspace_id,
            document_id=document_id,
            page=row.page,
            section=row.section,
            language=row.language or "unknown",
        )
        for row in chunk_rows
    ]

    # Extraction - deterministic, no model weights
    extractor = RuleBasedGraphExtractor()
    try:
        extraction_result = await extractor.extract(graph_chunks)
    except Exception as exc:
        raise RuntimeError(f"graph extraction failed: {exc}") from exc

    if extraction_result.errors:
        # Per-chunk errors are already recorded on result, but extraction as a
        # whole still succeeds for other chunks. Log and continue.
        for err in extraction_result.errors:
            logger.warning(
                "graph_chunk_extraction_error",
                extra={
                    "event": "graph_chunk_extraction_error",
                    "job_id": job_id,
                    "error": err.reason_code,
                },
            )

    # Community detection (T-V2-007): connected components, deterministic,
    # no new deps. Summaries via free model, bounded, with provenance.
    communities = []
    try:
        from graph.communities import detect_communities, summarize_community

        detected = detect_communities(
            extraction_result.entities, extraction_result.relationships, workspace_id
        )
        # Generate summaries for each community (free model, bounded)
        for comm in detected:
            member_ents = [e for e in extraction_result.entities if e.id in comm.entity_ids]
            member_rels = [
                r for r in extraction_result.relationships if r.id in comm.relationship_ids
            ]
            try:
                summary = await summarize_community(comm, member_ents, member_rels, workspace_id)
                # Bounded: 500 chars, provenance complete
                comm.summary = summary[:500] if len(summary) > 500 else summary
                # Title is already deterministic from detect_communities
            except Exception as exc:
                logger.warning(
                    "community_summary_failed",
                    extra={
                        "event": "community_summary_failed",
                        "job_id": job_id,
                        "community_id": comm.id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                # Fallback already handled inside summarize_community
                pass
            communities.append(comm)
        # Also include any extractor-provided communities (currently none, but keep)
        communities.extend(extraction_result.communities)
    except Exception as exc:
        logger.warning(
            "community_detection_failed",
            extra={
                "event": "community_detection_failed",
                "job_id": job_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        communities = list(extraction_result.communities)

    # Idempotent, atomically visible upsert (T-V2-002): single transaction
    # so a concurrent reader never sees a half-written extraction.
    # Communities now include detected + summarized, with provenance.
    store = SQLiteGraphStore(graph_db_path())
    try:
        await store.upsert_extraction(
            workspace_id,
            extraction_result.entities,
            extraction_result.relationships,
            communities,
        )
    finally:
        store.close()

    logger.info(
        "graph_extraction_indexed",
        extra={
            "event": "graph_extraction_indexed",
            "job_id": job_id,
            "workspace_id": workspace_id,
            "entities": len(extraction_result.entities),
            "relationships": len(extraction_result.relationships),
            "communities": len(extraction_result.communities),
        },
    )
    return {
        "entities": len(extraction_result.entities),
        "relationships": len(extraction_result.relationships),
        "communities": len(extraction_result.communities),
    }


# In-memory job state for retry tracking (when no DB table is used).
# For durability across worker restarts, this would be a DB table; for this
# ticket we keep it in redis hash graph:{prefix}job_state to survive claims.
async def _get_attempts(job_id: str) -> int:
    from graph.queue import _prefix, get_client

    try:
        raw = await get_client().hget(f"graph:{_prefix()}job_state", f"{job_id}:attempts")
        return int(raw) if raw else 0
    except Exception:
        return 0


async def _inc_attempts(job_id: str) -> int:
    from graph.queue import _prefix, get_client

    try:
        val = await get_client().hincrby(f"graph:{_prefix()}job_state", f"{job_id}:attempts", 1)
        return int(val)
    except Exception:
        return 1


async def _set_job_failed(job_id: str, error_code: str, message: str) -> None:
    from graph.queue import _prefix, get_client

    try:
        await get_client().hset(
            f"graph:{_prefix()}job_state",
            mapping={
                f"{job_id}:status": "failed",
                f"{job_id}:error_code": error_code,
                f"{job_id}:error_message": message[:2000],
            },
        )
    except Exception:
        pass


async def handle_graph_job(job_id: str) -> None:
    """Process one graph job with retry/park logic (T-V1-020 style)."""
    attempts = await _inc_attempts(job_id)
    try:
        await process_graph_job(job_id)
        # Success: clean up payload and state
        await delete_payload(job_id)
        from graph.queue import _prefix, get_client

        try:
            await get_client().hdel(
                f"graph:{_prefix()}job_state",
                f"{job_id}:attempts",
                f"{job_id}:status",
                f"{job_id}:error_code",
                f"{job_id}:error_message",
            )
        except Exception:
            pass
    except Exception as exc:
        # Retryable if attempts < max_attempts, else park as failed loud
        max_attempts = int(
            os.environ.get("GRAPH_MAX_ATTEMPTS", str(_DEFAULT_MAX_ATTEMPTS))
        )
        error_code = _GRAPH_ERROR_CODE
        message = f"{type(exc).__name__}: {exc}"
        if attempts < max_attempts:
            delay = min(2**attempts, _BACKOFF_CAP_SECONDS)
            try:
                await schedule_retry(job_id, delay)
                logger.warning(
                    "graph_job_retry",
                    extra={
                        "event": "graph_job_retry",
                        "job_id": job_id,
                        "attempts": attempts,
                        "delay": delay,
                        "error": error_code,
                    },
                )
            except GraphQueueUnavailable:
                logger.warning(
                    "graph_requeue_failed",
                    extra={
                        "event": "graph_requeue_failed",
                        "job_id": job_id,
                    },
                )
                raise
        else:
            await _set_job_failed(job_id, error_code, message)
            logger.warning(
                "graph_job_failed",
                extra={
                    "event": "graph_job_failed",
                    "job_id": job_id,
                    "attempts": attempts,
                    "error": error_code,
                },
            )
            # Do not re-queue; job will be acked and left as failed
            # The failure is LOUD: status failed, error_code visible,
            # not silent READY
            raise RuntimeError(
                f"graph job {job_id} parked as failed after "
                f"{attempts} attempts: {message}"
            ) from exc
        raise


async def _keep_alive(claimed: Claim) -> None:
    interval = max(2.0, 5.0)  # simple fixed interval, lease is 120s default
    # Use graph lease
    from graph.queue import lease_seconds

    interval = max(lease_seconds() / 3, 0.1)
    while True:
        try:
            if not await renew(claimed):
                logger.warning(
                    "graph_claim_lease_lost",
                    extra={
                        "event": "graph_claim_lease_lost",
                        "job_id": str(claimed),
                    },
                )
                return
        except GraphQueueUnavailable:
            logger.warning(
                "graph_claim_renew_unavailable",
                extra={
                    "event": "graph_claim_renew_unavailable",
                    "job_id": str(claimed),
                },
            )
        await asyncio.sleep(interval)


async def run_graph_worker(stop_event: asyncio.Event) -> None:
    """Graph worker loop - mirrors ingest_worker.run_worker but for graph queue."""
    logger.info("graph_worker_started", extra={"event": "graph_worker_started"})
    while not stop_event.is_set():
        try:
            await promote_due()
        except GraphQueueUnavailable:
            await asyncio.sleep(2)
            continue
        try:
            recovered = await recover_expired()
            if recovered:
                logger.warning(
                    "graph_claims_recovered",
                    extra={
                        "event": "graph_claims_recovered",
                        "count": recovered,
                    },
                )
        except GraphQueueUnavailable:
            pass
        try:
            claimed = await claim(timeout_seconds=1)
        except GraphQueueUnavailable:
            await asyncio.sleep(2)
            continue
        if claimed is None:
            continue
        heartbeat = asyncio.create_task(_keep_alive(claimed))
        try:
            job_id = str(claimed)
            try:
                await handle_graph_job(job_id)
            except Exception as exc:
                # If handle_graph_job parked as failed, it raised; we still need to
                # decide whether to ack or requeue. It already scheduled retry if
                # attempts < max, else it set failed state. In both cases we ack
                # the claim after durable outcome (retry queued or failed parked).
                # For retry case, the job is already in delayed zset, so ack is correct.
                # For park case, ack removes claim and leaves failed state.
                logger.error(
                    "graph_worker_error",
                    extra={
                        "event": "graph_worker_error",
                        "job_id": job_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                # If it was a retry, handle_graph_job already scheduled retry
                # If it was a park, we still ack to remove claim
                # Check if retry was scheduled: if attempts < max, job is in delayed
                # For now always ack after handle (retry already queued or parked)
                pass
            # Ack after durable outcome
            try:
                from graph.queue import ack as graph_ack

                await graph_ack(claimed)
                # Clean up payload if not already (success case already deleted)
                # For retry case, payload should remain for redelivery; but our
                # handle_graph_job for retry leaves payload intact and adds to delayed.
                # Ack will remove claim but payload stays; promote_due will requeue.
                # For success, payload already deleted.
                # For park, payload should be deleted? Currently not deleted on park,
                # but we should delete to avoid leaking.
                max_attempts = int(
                    os.environ.get(
                        "GRAPH_MAX_ATTEMPTS", str(_DEFAULT_MAX_ATTEMPTS)
                    )
                )
                if await _get_attempts(job_id) >= max_attempts:
                    await delete_payload(job_id)
            except GraphQueueUnavailable:
                pass
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
