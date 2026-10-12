"""Worker loop + retry/failure policy for ingestion jobs (M2-001c).

T-V1-026 (trace propagation): the queue carries the ``job_id``, and that id is
the ingestion tracking ID. :func:`process_one` binds it to the structured-log
correlation slot for the whole job, so every stage, every store call and every
failure record written by this worker — including the terminal
``error_code``/``error_message`` on the job row — carries the same id the
``/jobs/{id}`` poll returns. Ingress → queue → worker → stores is therefore one
joinable trace with no schema change (the job id already crosses every one of
those boundaries).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from core.errors import normalize_code
from core.logging import request_id_ctx, set_request_id
from ingestion.persistence import (
    IngestionJob,
    JobStage,
    JobStatus,
    get_session_factory,
)
from ingestion.queue import IngestQueueUnavailable, dequeue, promote_due, schedule_retry
from sqlalchemy.ext.asyncio import AsyncSession

from jobs.ingest_pipeline import IngestPipelineError, process_job

logger = logging.getLogger("knowledge_os.worker.ingest")

_BACKOFF_CAP_SECONDS = 30
_UNHANDLED_RETRY_SECONDS = 2.0


async def record_failure(
    session: AsyncSession, job_id: uuid.UUID, exc: IngestPipelineError
) -> None:
    """Retry retryable failures with backoff; park poison messages as failed."""
    job = await session.get(IngestionJob, job_id)
    if job is None:
        return
    #: T-V1-025: persist through the shared taxonomy registry so DB and the
    #: job-poll API speak one code-set. ``normalize_code`` is an identity for
    #: every code the pipeline currently emits (legacy wire values are
    #: registered as-is), so this changes no stored value today.
    job.error_code = normalize_code(exc.code)
    job.error_message = (exc.message or "")[:2000]
    trace_id = request_id_ctx.get()

    if exc.retryable and job.attempts < job.max_attempts:
        job.status = JobStatus.QUEUED.value
        job.stage = JobStage.QUEUED.value
        await session.commit()
        delay = min(2**job.attempts, _BACKOFF_CAP_SECONDS)
        await schedule_retry(str(job.id), delay)
        logger.warning(
            "ingest_job_retry",
            extra={
                "event": "ingest_job_retry",
                "job_id": str(job.id),
                "attempts": job.attempts,
                "delay": delay,
                "error": exc.code,
                "trace_id": trace_id,
            },
        )
    else:
        job.status = JobStatus.FAILED.value
        job.stage = JobStage.FAILED.value
        job.finished_at = datetime.now(UTC)
        await session.commit()
        logger.warning(
            "ingest_job_failed",
            extra={
                "event": "ingest_job_failed",
                "job_id": str(job.id),
                "attempts": job.attempts,
                "error": exc.code,
                "trace_id": trace_id,
            },
        )


async def process_one(job_id: uuid.UUID) -> None:
    # T-V1-026: bind the tracking ID for the whole job. The queue entry IS the
    # job id, so this single binding is what joins ingress (the upload that
    # created the job) to every worker stage and store write below it.
    set_request_id(str(job_id))
    async with get_session_factory()() as session:
        try:
            await process_job(session, job_id)
        except IngestPipelineError as exc:
            await session.rollback()
            await record_failure(session, job_id, exc)
        except Exception as exc:  # unexpected -> retryable
            await session.rollback()
            await record_failure(
                session,
                job_id,
                IngestPipelineError(
                    "internal_error", f"{type(exc).__name__}: {exc}", retryable=True
                ),
            )


async def run_worker(stop_event: asyncio.Event) -> None:
    """Consume ingestion jobs until ``stop_event`` is set."""
    logger.info("ingest_worker_started", extra={"event": "ingest_worker_started"})
    while not stop_event.is_set():
        try:
            await promote_due()
            job_id = await dequeue(timeout_seconds=1)
        except IngestQueueUnavailable:
            logger.warning(
                "ingest_queue_unavailable", extra={"event": "ingest_queue_unavailable"}
            )
            await asyncio.sleep(2)
            continue
        if job_id is None:
            continue
        try:
            await process_one(uuid.UUID(job_id))
        except ValueError:
            logger.warning(
                "ingest_bad_job_id",
                extra={"event": "ingest_bad_job_id", "job_id": job_id},
            )
        except Exception as exc:
            # Catch-all: a failure escaping process_one (e.g. Postgres down while
            # record_failure runs) must never kill the loop. Requeue best-effort
            # so the dequeued job is not lost, then back off briefly.
            logger.error(
                "ingest_worker_error",
                extra={
                    "event": "ingest_worker_error",
                    "job_id": job_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            try:
                await schedule_retry(job_id, _UNHANDLED_RETRY_SECONDS)
            except Exception:
                logger.warning(
                    "ingest_requeue_failed",
                    extra={"event": "ingest_requeue_failed", "job_id": job_id},
                )
            await asyncio.sleep(1)
