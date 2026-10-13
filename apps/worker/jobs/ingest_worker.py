"""Worker loop + retry/failure policy for ingestion jobs (M2-001c).

T-V1-026 (trace propagation): the queue carries the ``job_id``, and that id is
the ingestion tracking ID. :func:`process_one` binds it to the structured-log
correlation slot for the whole job, so every stage, every store call and every
failure record written by this worker — including the terminal
``error_code``/``error_message`` on the job row — carries the same id the
``/jobs/{id}`` poll returns. Ingress → queue → worker → stores is therefore one
joinable trace with no schema change (the job id already crosses every one of
those boundaries).

T-V1-020 (durable claim/ack): the loop no longer BRPOPs a job out of Redis
(and thereby out of existence). Each fetch is a *claim* — the job moves onto a
lease-scored claims zset with a fence token — and the claim is released
(:func:`ack`) **only after a durable outcome is committed**: READY, a terminal
FAILED record, or a re-queued retry (delayed-zset entry). A worker that dies
mid-job keeps its claim un-acked; the lease expires and
``recover_expired`` redelivers (at-least-once). Redelivery is safe because
publication is idempotent (T-V1-016 stable IDs) and fenced against dead
generations (T-V1-019). A heartbeat (:func:`renew`) extends the lease while a
healthy job runs past the base lease.

T-V1-046 (deterministic failures park, never retry): :func:`record_failure`
takes its verdict from :func:`classify_pipeline_failure`, so a storage-boundary
vector rejection (T-V1-015's typed ``VectorStoreInvalidVector``) becomes a
terminal ``ZERO_VECTOR_REJECTED`` on the first attempt instead of being
retried into the ground, while genuinely transient failures keep their
retryable verdict and their backoff.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

from core.errors import ReasonCode, normalize_code
from core.logging import request_id_ctx, set_request_id

# T-V1-020: claim/ack/lease live behind the adapter's typed surface. They are
# not yet re-exported by ``ingestion.queue`` (outside this ticket's allowed
# files) — see handoff "recommended follow-up".
from ingestion.adapters.redis_queue import (
    Claim,
    ack,
    claim,
    lease_seconds,
    recover_expired,
    renew,
)
from ingestion.persistence import (
    IngestionJob,
    JobStage,
    JobStatus,
    get_session_factory,
)
from ingestion.queue import IngestQueueUnavailable, promote_due, schedule_retry
from sqlalchemy.ext.asyncio import AsyncSession

from jobs.ingest_pipeline import IngestPipelineError, process_job

logger = logging.getLogger("knowledge_os.worker.ingest")

_BACKOFF_CAP_SECONDS = 30
_UNHANDLED_RETRY_SECONDS = 2.0

#: The loop's historical fetch seam name. T-V1-020: this is now
#: ``claim`` under the hood — it returns a :class:`Claim` (job id + lease
#: fence token), not a bare popped id. The name is kept because existing
#: worker-loop tests pin ``run_worker``'s consumption seam as ``dequeue``.
dequeue = claim


# T-V1-046 — deterministic validation failures park, never retry -------------
#
# The storage boundary (T-V1-015, ``retrieval.adapters.qdrant_store``) rejects
# an invalid embedding *before* any point is built or sent, raising the typed
# ``VectorStoreInvalidVector`` with one of the stable ``REJECT_*`` reasons
# (``dimension_mismatch`` / ``non_finite`` / ``zero_vector``). Every one of
# those is a property of the *data*, not of the service: a retry replays the
# exact same vector, so the retry is doomed by construction.
#
# The pipeline's generic ``except Exception`` on the upsert leg wraps that
# rejection as a retryable ``QDRANT_INDEXING_FAILED`` (the nuance T-V1-044
# disclosed and asked to be ruled on), so the job burned its whole retry
# budget before parking anyway. :func:`classify_pipeline_failure` is the ONE
# place that puts the verdict back — the shared error handling, not a
# per-caller guard in the pipeline, which is what keeps "single boundary, not
# per-caller checks" (T-V1-044) true. The job then fails fast-and-final under
# the registry's own non-retryable code (``ZERO_VECTOR_REJECTED``:
# ``core.errors`` → INDEXING / 422 / retryable=False), with the precise
# ``REJECT_*`` reason preserved in the message. Genuinely transient failures
# (Qdrant unreachable, a 5xx, a timeout) keep the pipeline's verdict and are
# still retried.


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield ``exc`` plus everything it caused, without looping on a cycle.

    Both ``__cause__`` (explicit ``raise … from``) and ``__context__`` (a bare
    re-raise inside an ``except`` block) count as "caused by", so a rejection
    is recognised no matter which of the two a wrapping leg preserved.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def classify_pipeline_failure(exc: IngestPipelineError) -> IngestPipelineError:
    """Final (code, retryable) verdict for one pipeline failure.

    Returns the failure unchanged unless a storage-boundary vector rejection
    is found anywhere in its cause chain, in which case it is re-issued as the
    canonical non-retryable ``ZERO_VECTOR_REJECTED``. Everything else — every
    transient store/dependency failure — keeps the pipeline's own verdict, so
    retry behaviour for those is byte-identical to before.

    The adapter import is lazy on purpose, matching ``ingest_pipeline`` and
    ``ingestion.dedup``: importing the store at module scope would pull the
    Qdrant SDK into every ``import jobs.ingest_worker``, including the
    test-only seams that never touch a vector store.
    """
    from retrieval.adapters.qdrant_store import VectorStoreInvalidVector

    for chained in _exception_chain(exc):
        if isinstance(chained, VectorStoreInvalidVector):
            return IngestPipelineError(
                ReasonCode.ZERO_VECTOR_REJECTED.value,
                f"storage boundary rejected the embedding ({chained.reason}): {chained}",
                retryable=False,
            )
    return exc


async def record_failure(
    session: AsyncSession, job_id: uuid.UUID, exc: IngestPipelineError
) -> None:
    """Retry retryable failures with backoff; park poison messages as failed.

    T-V1-046: the verdict is taken once, up front, by
    :func:`classify_pipeline_failure`, so the code persisted below and the
    retry/park decision taken after it can never disagree — a deterministic
    boundary rejection is neither parked under a retryable code nor retried
    under a non-retryable one.
    """
    exc = classify_pipeline_failure(exc)
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
                    "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", retryable=True
                ),
            )


async def _ack_or_log(claimed: Claim | str) -> None:
    """Release a claim once — and only once — its outcome is durable.

    T-V1-020 reviewer checklist (1): every call site of this helper sits
    *after* a committed DB record (READY, terminal FAILED, or QUEUED + delayed
    re-queue). A plain ``str`` payload (legacy/test dequeue) carries no token
    and has no claim to release. A fenced ``False`` means our lease already
    expired and the job was re-claimed elsewhere — the other holder's delivery
    stands; we only log.
    """
    if not isinstance(claimed, Claim):
        return
    try:
        released = await ack(claimed)
    except IngestQueueUnavailable:
        # Nothing durable is at risk: the job's outcome is already committed.
        # The un-acked claim expires and recovery redelivers (at-least-once).
        logger.warning(
            "ingest_ack_unavailable",
            extra={"event": "ingest_ack_unavailable", "job_id": str(claimed)},
        )
        return
    if not released:
        logger.warning(
            "ingest_ack_fenced",
            extra={
                "event": "ingest_ack_fenced",
                "job_id": str(claimed),
                "note": "lease expired and job re-claimed; other holder stands",
            },
        )


async def _keep_alive(claimed: Claim | str) -> None:
    """Heartbeat: extend the lease while a healthy job runs (T-V1-020).

    A long embed/index must not lose its claim to the lease clock. Renewal is
    token-fenced: once the lease has expired and the job was re-claimed,
    ``renew`` returns False and this holder stops pretending to own the job.
    """
    if not isinstance(claimed, Claim):
        return
    interval = max(lease_seconds() / 3, 0.1)
    while True:
        # Renew first (the claim was just taken, extend it immediately), then
        # keep the interval running while the job lives.
        try:
            if not await renew(claimed):
                logger.warning(
                    "ingest_claim_lease_lost",
                    extra={
                        "event": "ingest_claim_lease_lost",
                        "job_id": str(claimed),
                        "note": "renew fenced; claim was lost to expiry/recovery",
                    },
                )
                return
        except IngestQueueUnavailable:
            # Transient: keep heartbeating; the lease survives a few misses.
            logger.warning(
                "ingest_claim_renew_unavailable",
                extra={"event": "ingest_claim_renew_unavailable", "job_id": str(claimed)},
            )
        await asyncio.sleep(interval)


async def run_worker(stop_event: asyncio.Event) -> None:
    """Consume ingestion jobs until ``stop_event`` is set.

    T-V1-020: consumption is claim→process→ack, never a destructive pop.
    The claim is released only after the durable outcome below; if this
    process dies before that, the lease expires and ``recover_expired``
    redelivers the job (at-least-once; publication is idempotent via the
    T-V1-016 stable IDs).
    """
    logger.info("ingest_worker_started", extra={"event": "ingest_worker_started"})
    while not stop_event.is_set():
        try:
            await promote_due()
        except IngestQueueUnavailable:
            logger.warning(
                "ingest_queue_unavailable", extra={"event": "ingest_queue_unavailable"}
            )
            await asyncio.sleep(2)
            continue
        try:
            # Reviewer checklist (2): every expired claim goes back on the
            # ready queue each tick, before we claim anything new.
            recovered = await recover_expired()
            if recovered:
                logger.warning(
                    "ingest_claims_recovered",
                    extra={
                        "event": "ingest_claims_recovered",
                        "count": recovered,
                        "note": "expired claims re-queued (dead-worker recovery)",
                    },
                )
        except IngestQueueUnavailable:
            # Best-effort sweep: consumption below still applies its own
            # queue-unavailable backoff if Redis is truly down.
            logger.warning(
                "ingest_claim_recovery_unavailable",
                extra={"event": "ingest_claim_recovery_unavailable"},
            )
        try:
            claimed = await dequeue(timeout_seconds=1)
        except IngestQueueUnavailable:
            logger.warning(
                "ingest_queue_unavailable", extra={"event": "ingest_queue_unavailable"}
            )
            await asyncio.sleep(2)
            continue
        if claimed is None:
            continue
        try:
            job_id = uuid.UUID(str(claimed))
        except ValueError:
            logger.warning(
                "ingest_bad_job_id",
                extra={"event": "ingest_bad_job_id", "job_id": str(claimed)},
            )
            # A payload that is not a job id can never be processed: dropping
            # the claim IS its terminal outcome (the warning above is the
            # record). Leaving it claimed would only re-deliver garbage.
            await _ack_or_log(claimed)
            continue
        heartbeat = asyncio.create_task(_keep_alive(claimed))
        try:
            await process_one(job_id)
        except Exception as exc:
            # Catch-all: a failure escaping process_one (e.g. Postgres down
            # while record_failure runs) committed NO durable outcome, so the
            # claim must NOT be acked. Requeue fast for retry, and ack only
            # once the delayed zset owns the job; if even that fails, the
            # un-acked claim expires and recovery redelivers (T-V1-020).
            logger.error(
                "ingest_worker_error",
                extra={
                    "event": "ingest_worker_error",
                    "job_id": str(claimed),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            requeued = False
            try:
                await schedule_retry(str(claimed), _UNHANDLED_RETRY_SECONDS)
                requeued = True
            except Exception:
                logger.warning(
                    "ingest_requeue_failed",
                    extra={"event": "ingest_requeue_failed", "job_id": str(claimed)},
                )
            if requeued:
                await _ack_or_log(claimed)
            await asyncio.sleep(1)
        else:
            # process_one returned normally: the durable outcome is committed
            # (READY, terminal FAILED, or QUEUED + delayed retry via
            # record_failure) — ack-after-durability, checklist (1).
            await _ack_or_log(claimed)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
