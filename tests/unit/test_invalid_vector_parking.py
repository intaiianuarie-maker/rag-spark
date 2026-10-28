"""T-V1-046 — deterministic validation failures park, never retry.

The storage boundary (T-V1-015) rejects an invalid embedding *before* any
point is built or sent, so a rejection is a property of the vector itself:
retrying replays the same input and is doomed by construction. Until this
ticket the pipeline's generic ``except Exception`` on the upsert leg wrapped
that rejection as a retryable ``QDRANT_INDEXING_FAILED`` (T-V1-044's disclosed
nuance), so a job with a bad embedding burned its whole retry budget before
parking anyway.

Three properties are pinned here, service-free (no Redis, no Postgres, no
Qdrant — the fakes are in-file and the store runs on a stub client):

1. **Deterministic parks** — every ``REJECT_*`` boundary reason, arriving
   wrapped exactly as the pipeline wraps it, is terminal on the FIRST attempt:
   status ``failed``, code ``ZERO_VECTOR_REJECTED`` (the registry's
   non-retryable INDEXING/422 code), ``schedule_retry`` never called, and the
   precise boundary reason still readable in ``error_message``.
2. **Transient stays retryable** — a genuinely transient upsert failure (a
   connection error surfaced as ``VectorStoreError``) keeps the pipeline's
   verdict: status ``queued``, backoff scheduled, code unmoved. Retry
   behaviour for transient failures is byte-identical to before this ticket.
3. **Zero writes either way** — a rejected vector makes the boundary perform
   zero client upsert calls (a valid control batch still lands, so the
   zero-write assertion is not vacuous), and parking schedules zero retries,
   so the invalid vector is neither written now nor re-attempted later.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from core.errors import ERROR_REGISTRY, ReasonCode
from retrieval.adapters.qdrant_store import (
    REJECT_DIMENSION,
    REJECT_NON_FINITE,
    REJECT_ZERO,
    SPARSE_NAME,
    QdrantVectorStore,
    VectorStoreError,
    VectorStoreInvalidVector,
)
from retrieval.embedding_identity import EmbeddingIdentity
from retrieval.interfaces import VectorRecord

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

import jobs.ingest_worker as ingest_worker  # env set-up above must come first
from jobs.ingest_pipeline import IngestPipelineError

pytestmark = pytest.mark.unit

DIM = 4
COLL = "knowledge"

#: Every deterministic reason the storage boundary can refuse a vector.
ALL_REJECT_REASONS = (REJECT_ZERO, REJECT_NON_FINITE, REJECT_DIMENSION)


# --------------------------------------------------------------------------------------
# Failure shapes — built the way the real pipeline builds them
# --------------------------------------------------------------------------------------


def _invalid_vector(reason: str, record_id: str = "chk-1") -> VectorStoreInvalidVector:
    """The typed boundary error, with the reason the boundary would emit."""
    return VectorStoreInvalidVector(
        reason, f"record {record_id}: {reason} — refusing to write", record_id=record_id
    )


def _pipeline_wrap(reason: str) -> IngestPipelineError:
    """A boundary rejection exactly as the pipeline's upsert leg raises it.

    ``apps/worker/jobs/ingest_pipeline.py`` wraps *every* upsert exception as
    a retryable ``QDRANT_INDEXING_FAILED`` with the original error as its
    cause, so this is the shape the worker actually receives. It is built here
    rather than by executing the pipeline (which needs Redis, Postgres and
    Qdrant): the mapping under test is over the failure shape, and building it
    keeps this suite service-free per tests/README.

    ``__cause__`` is set directly because ``raise … from …`` is a statement,
    not an expression — this is exactly what it stores.
    """
    boundary = _invalid_vector(reason)
    error = IngestPipelineError(
        "QDRANT_INDEXING_FAILED",
        f"Vector store upsert failed: {boundary}",
        retryable=True,
    )
    error.__cause__ = boundary
    return error


def _transient_wrap() -> IngestPipelineError:
    """A genuinely transient upsert failure: connection refused, not bad data."""
    transport = VectorStoreError("upsert failed for knowledge: connection refused")
    error = IngestPipelineError(
        "QDRANT_INDEXING_FAILED",
        f"Vector store upsert failed: {transport}",
        retryable=True,
    )
    error.__cause__ = transport
    return error


# --------------------------------------------------------------------------------------
# Harness — a scripted job row + a retry scheduler that records its calls
# --------------------------------------------------------------------------------------


class _Job:
    """The columns ``record_failure`` reads and writes."""

    def __init__(self, *, attempts: int = 0, max_attempts: int = 3) -> None:
        self.id = uuid.uuid4()
        self.attempts = attempts
        self.max_attempts = max_attempts
        self.status = "running"
        self.stage = "embedding"
        self.error_code: str | None = None
        self.error_message: str | None = None
        self.finished_at: datetime | None = None


class _Session:
    def __init__(self, job: _Job) -> None:
        self._job = job
        self.events: list[str] = []

    async def get(self, model: Any, job_id: uuid.UUID) -> _Job:
        return self._job

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")


async def _run_record_failure(
    monkeypatch: pytest.MonkeyPatch,
    exc: IngestPipelineError,
    *,
    attempts: int = 0,
    max_attempts: int = 3,
) -> tuple[_Job, list[str], list[tuple[str, float]]]:
    """Drive the real ``record_failure``; return (job, events, scheduled retries)."""
    job = _Job(attempts=attempts, max_attempts=max_attempts)
    session = _Session(job)
    scheduled: list[tuple[str, float]] = []

    async def fake_schedule_retry(job_id: str, delay: float) -> None:
        scheduled.append((job_id, delay))
        session.events.append("schedule_retry")

    monkeypatch.setattr(ingest_worker, "schedule_retry", fake_schedule_retry)
    await ingest_worker.record_failure(session, job.id, exc)
    return job, session.events, scheduled


# --------------------------------------------------------------------------------------
# 1. Deterministic validation failures park — on the first attempt
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ALL_REJECT_REASONS, ids=ALL_REJECT_REASONS)
async def test_invalid_vector_rejection_parks_without_retrying(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """Reviewer checklist (1): doomed retries = 0 — the job is final at once."""
    job, events, scheduled = await _run_record_failure(monkeypatch, _pipeline_wrap(reason))

    assert job.status == "failed", "a deterministic rejection must park, not requeue"
    assert job.error_code == "ZERO_VECTOR_REJECTED"
    assert ERROR_REGISTRY[ReasonCode.ZERO_VECTOR_REJECTED].retryable is False
    assert scheduled == [], "a doomed retry must never be scheduled"
    assert "schedule_retry" not in events
    assert job.finished_at is not None, "the park must leave a terminal timestamp"
    # The canonical code is one label for three reasons — the precise boundary
    # reason stays readable for the operator instead of being flattened away.
    assert reason in (job.error_message or "")


@pytest.mark.parametrize("reason", ALL_REJECT_REASONS, ids=ALL_REJECT_REASONS)
def test_classifier_marks_every_boundary_reason_non_retryable(reason: str) -> None:
    """The mapping itself: every REJECT_* reason resolves to one park code."""
    classified = ingest_worker.classify_pipeline_failure(_pipeline_wrap(reason))

    assert classified.code == "ZERO_VECTOR_REJECTED"
    assert classified.retryable is False


async def test_a_rejection_is_recognised_through_an_outer_wrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verdict is taken over the whole cause chain, not just the top link."""
    inner = _pipeline_wrap(REJECT_ZERO)
    outer = IngestPipelineError(
        "INTERNAL_ERROR", "embedding and vector store indexing failed", retryable=True
    )
    outer.__cause__ = inner

    job, _, scheduled = await _run_record_failure(monkeypatch, outer)

    assert job.status == "failed"
    assert job.error_code == "ZERO_VECTOR_REJECTED"
    assert scheduled == []


def test_classifier_is_cycle_safe() -> None:
    """A pathological cause cycle must not hang the mapping."""
    exc = _pipeline_wrap(REJECT_ZERO)
    exc.__cause__.__context__ = exc  # type: ignore[union-attr]

    classified = ingest_worker.classify_pipeline_failure(exc)

    assert classified.code == "ZERO_VECTOR_REJECTED"


# --------------------------------------------------------------------------------------
# 2. Genuinely transient failures stay retryable
# --------------------------------------------------------------------------------------


async def test_transient_upsert_failure_is_still_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer checklist (2): transient retries preserved, verdict and code unmoved."""
    job, _, scheduled = await _run_record_failure(monkeypatch, _transient_wrap())

    assert job.status == "queued", "a transient failure must be re-queued, not parked"
    assert job.error_code == "QDRANT_INDEXING_FAILED"  # the pipeline's own code
    assert scheduled == [(str(job.id), 1)]  # 2**attempts(0) = 1s, unchanged backoff
    assert job.finished_at is None


def test_classifier_leaves_every_transient_verdict_untouched() -> None:
    """Non-boundary failures return unchanged — for both retryable verdicts."""
    for retryable in (True, False):
        exc = IngestPipelineError("QDRANT_INDEXING_FAILED", "boom", retryable=retryable)
        assert ingest_worker.classify_pipeline_failure(exc) is exc


async def test_transient_still_parks_once_its_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry-exhaustion parking (pre-existing behaviour) is untouched."""
    job, _, scheduled = await _run_record_failure(
        monkeypatch, _transient_wrap(), attempts=3, max_attempts=3
    )

    assert job.status == "failed"
    assert job.error_code == "QDRANT_INDEXING_FAILED"
    assert scheduled == []


# --------------------------------------------------------------------------------------
# 3. Zero writes either way
# --------------------------------------------------------------------------------------


class _StubQdrantClient:
    """Minimal write-path stand-in: records every mutating call it receives."""

    def __init__(self) -> None:
        self.dimensions: dict[str, int] = {}
        self.points: dict[str, dict[str, object]] = {}
        self.upsert_calls: list[tuple[str, list[object]]] = []

    def seed(self, name: str, *, points: int = 3, dimension: int = DIM) -> None:
        self.dimensions[name] = dimension
        self.points[name] = {f"seed-{i}": object() for i in range(points)}

    async def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self.dimensions

    async def get_collection(self, collection_name: str) -> SimpleNamespace:
        if collection_name not in self.dimensions:
            raise KeyError(collection_name)
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={"dense": SimpleNamespace(size=self.dimensions[collection_name])},
                    # Faithful to a live hybrid collection (T-I09 sparse-declared
                    # check): the named sparse vector is declared alongside dense.
                    sparse_vectors={SPARSE_NAME: SimpleNamespace()},
                )
            ),
            points_count=len(self.points.get(collection_name, {})),
        )

    async def create_collection(self, *, collection_name: str, **kwargs: Any) -> None:
        self.dimensions[collection_name] = int(kwargs["vectors_config"]["dense"].size)
        self.points.setdefault(collection_name, {})

    async def upsert(self, *, collection_name: str, points: list[Any], **kwargs: Any) -> None:
        self.upsert_calls.append((collection_name, points))
        stored = self.points.setdefault(collection_name, {})
        for point in points:
            stored[point.id] = point

    async def close(self) -> None:
        return None


def _make_store() -> tuple[QdrantVectorStore, _StubQdrantClient]:
    client = _StubQdrantClient()
    store = QdrantVectorStore(collection=COLL, client=client)  # type: ignore[arg-type]
    client.seed(COLL)
    return store, client


#: T-R06b: records reaching the boundary must carry embedding identity.
IDENTITY = EmbeddingIdentity(
    provider="ParkingStub", model="stub-model", revision="stub-rev1", dimension=DIM
)


def _record(
    *, id: str = "chk-1", vector: list[float], metadata: dict | None = None
) -> VectorRecord:
    return VectorRecord(
        id=id,
        vector=vector,
        content="evidence",
        workspace_id="ws-a",
        metadata=IDENTITY.as_metadata() if metadata is None else metadata,
    )


async def test_boundary_writes_nothing_for_any_invalid_vector() -> None:
    """Reviewer checklist (3a): a rejected vector reaches no client call."""
    store, client = _make_store()
    await store.ensure_collection(collection=COLL, dimension=DIM)
    invalid = (
        [0.0] * DIM,  # zero
        [0.1, float("nan"), 0.3, 0.4],  # non-finite
        [0.1, 0.2, 0.3],  # wrong dimension
    )

    for vector in invalid:
        with pytest.raises(VectorStoreInvalidVector):
            await store.upsert(collection=COLL, records=[_record(vector=vector)])

    assert client.upsert_calls == [], "a rejected vector must cause ZERO writes"
    assert set(client.points[COLL]) == {"seed-0", "seed-1", "seed-2"}, "index must be untouched"


async def test_valid_batch_still_lands_so_the_zero_write_is_not_vacuous() -> None:
    """Control: the stub really is reached when the vector is valid."""
    store, client = _make_store()
    await store.ensure_collection(collection=COLL, dimension=DIM)

    written = await store.upsert(
        collection=COLL, records=[_record(id="ok-1", vector=[0.5] * DIM)]
    )

    assert written == 1
    assert len(client.upsert_calls) == 1


@pytest.mark.parametrize("reason", ALL_REJECT_REASONS, ids=ALL_REJECT_REASONS)
async def test_a_rejected_vector_is_neither_written_nor_re_attempted(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """Checklist (3b), end to end over the shape: nothing written, nothing retried.

    Park path and retry path are asserted together for the same failure: the
    boundary refuses the write (zero client calls) *and* the worker schedules
    no retry, so the invalid vector can never land — now or on a redelivery.
    """
    store, client = _make_store()
    await store.ensure_collection(collection=COLL, dimension=DIM)

    with pytest.raises(VectorStoreInvalidVector):
        await store.upsert(collection=COLL, records=[_record(vector=[0.0] * DIM)])
    job, _, scheduled = await _run_record_failure(monkeypatch, _pipeline_wrap(reason))

    assert client.upsert_calls == []
    assert job.status == "failed"
    assert job.error_code == "ZERO_VECTOR_REJECTED"
    assert scheduled == []
