"""T-V1-050 — the publish decision is atomic in the DATABASE, not in app code.

T-V1-019's publication gate is **read-then-write**: it SELECTs the authority
(``documents`` / ``document_versions``), the pipeline then keeps working —
graph extraction, lost-region disclosure — and only finally commits READY. A
replace or delete landing between that SELECT and that commit publishes over a
version the job never validated (T-V1-019 risk 1).

The fix moves the decision into the ``UPDATE`` that performs it: the publish
statement re-asserts the fence in its **own WHERE clause**, so Postgres
evaluates predicate and status change as one atomic step. Losing the race
updates 0 rows and the job fails loudly instead of silently publishing stale
state.

Pinned here, service-free (a stub session; the SQL is asserted by compiling
the real SQLAlchemy statement, not by inspecting app code):

1. the fence lives **inside** the publish statement (DB-enforced, checklist 3);
2. a losing race updates 0 rows and refuses loudly (checklist 1);
3. the normal single-writer path is unchanged (checklist 2).
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy.dialects import postgresql

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

import jobs.ingest_pipeline as ingest_pipeline
from ingestion.persistence import IngestionJob, JobStage, JobStatus
from jobs.ingest_pipeline import (
    FENCE_DELETED,
    FENCE_SUPERSEDED,
    IngestPipelineError,
    _generation_still_live,
    publish_job_if_generation_live,
)

pytestmark = pytest.mark.unit

VERSION_ID = uuid.UUID("00000000-0000-0000-0000-000000000050")


def _publish_statement(job_id: uuid.UUID | None = None):
    """The real statement ``publish_job_if_generation_live`` issues."""
    from datetime import UTC, datetime

    from sqlalchemy import update

    return (
        update(IngestionJob)
        .where(
            IngestionJob.id == (job_id or uuid.uuid4()),
            _generation_still_live(VERSION_ID),
        )
        .values(
            status=JobStatus.READY.value,
            stage=JobStage.READY.value,
            finished_at=datetime.now(UTC),
        )
        .execution_options(synchronize_session=False)
    )


def _sql() -> str:
    return str(_publish_statement().compile(dialect=postgresql.dialect()))


# --------------------------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------------------------


class _Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _Job:
    id = uuid.UUID("00000000-0000-0000-0000-000000000099")
    document_id = uuid.UUID("00000000-0000-0000-0000-000000000011")


class _Session:
    """Enough of an AsyncSession for the CAS: rowcount in, calls recorded."""

    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount
        self.executed: list[object] = []
        self.expired: list[object] = []
        self.unpublished = 0

    async def execute(self, stmt, params=None):
        self.executed.append(stmt)
        return _Result(self.rowcount)

    def expire(self, obj) -> None:
        # Sync, like AsyncSession.expire — the CAS must not await it.
        self.expired.append(obj)


# --------------------------------------------------------------------------------------
# 1. The guard is DB-enforced: the fence is INSIDE the publish statement
# --------------------------------------------------------------------------------------


def test_the_publish_where_clause_carries_the_whole_fence() -> None:
    """Checklist 3: predicate and status change are one statement, not two steps."""
    sql = _sql()

    assert sql.startswith("UPDATE ingestion_jobs"), "it must be the publish statement itself"
    assert "WHERE ingestion_jobs.id" in sql, "keyed to the job row"
    assert " AND (EXISTS (" in sql, "the fence is a predicate on that UPDATE"
    # The three conditions, all inside the statement:
    assert "documents.deleted_at IS NULL" in sql
    assert "document_versions.superseded_at IS NULL" in sql
    assert "documents.current_version_id = document_versions.id" in sql
    assert "documents.current_version_id IS NULL" in sql, "a NULL pointer makes no claim"


def test_the_publish_sets_exactly_the_terminal_fields() -> None:
    """Checklist 2: the normal path writes the same three fields as before."""
    sql = _sql()

    assert "SET" in sql
    for assignment in ("status=", "stage=", "finished_at="):
        assert assignment in sql.replace(" ", ""), f"{assignment} missing"
    assert "ingestion_jobs.status" not in sql.split("SET", 1)[1], (
        "status must be assigned, never filtered on — the CAS keys on the fence"
    )


def test_the_fence_is_deterministic_no_clock_and_no_sleep() -> None:
    """Same rule T-V1-019 pinned: a generation compare, never a timing guess.

    The only ``now()`` in the statement is SQLAlchemy's automatic
    ``updated_at`` onupdate, which is not part of the fence.
    """
    sql = _sql()
    fence = sql.split("AND (EXISTS (", 1)[1]
    assert "now()" not in fence
    assert "pg_sleep" not in fence


def test_the_identity_map_cannot_flush_a_competing_update() -> None:
    """The CAS must keep the identity map in step **without** a second write.

    T-V1-059 regression: the original implementation used
    ``synchronize_session=False`` + ``session.expire(job)``, so the next
    attribute access issued a refresh SELECT outside a greenlet and every
    publish died with ``MissingGreenlet`` (a retryable INTERNAL_ERROR, so the
    job looped instead of going READY). ``synchronize_session="fetch"`` does
    the sync *inside* ``execute()``, where the greenlet is live, and applies
    the fetched values as clean state — so neither an unpredicated UPDATE nor
    a post-hoc refresh can happen.
    """
    import inspect

    raw = inspect.getsource(publish_job_if_generation_live)
    # Strip comments: the docstring/comment above *names* the old
    # ``session.expire`` deliberately, and prose is not executable code.
    source = "\n".join(line.split("#", 1)[0] for line in raw.splitlines())
    assert 'synchronize_session="fetch"' in source, "sync inside execute(), not after it"
    assert "session.expire" not in source, (
        "expiring here re-opens the MissingGreenlet publish failure (T-V1-059)"
    )


# --------------------------------------------------------------------------------------
# 2. Both directions of the race
# --------------------------------------------------------------------------------------


async def test_a_live_generation_publishes() -> None:
    """Normal single-writer path: the predicate matches, 1 row updated."""
    session = _Session(rowcount=1)
    job = _Job()

    published = await publish_job_if_generation_live(
        session, job=job, version_id=VERSION_ID  # type: ignore[arg-type]
    )

    assert published is True
    assert len(session.executed) == 1, "one statement, one round-trip"
    assert session.expired == [], (
        "no post-hoc expire/refresh: it would issue IO outside the greenlet "
        "and fail the publish (T-V1-059 / MissingGreenlet)"
    )


async def test_a_concurrent_replace_publishes_nothing() -> None:
    """Checklist 1: the replace wins the race ⇒ 0 rows ⇒ NOT published."""
    session = _Session(rowcount=0)

    published = await publish_job_if_generation_live(
        session, job=_Job(), version_id=VERSION_ID  # type: ignore[arg-type]
    )

    assert published is False, "0 rows updated must never read as published"


async def test_losing_the_race_fails_loudly_with_a_fence_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal is visible: a fence code, non-retryable, and unpublished."""
    session = _Session(rowcount=0)
    unpublished: list[tuple] = []

    async def _state(*args, **kwargs):
        return FENCE_SUPERSEDED  # the authority now reports the replace

    async def _unpublish(*args, **kwargs):
        unpublished.append(kwargs)

    monkeypatch.setattr(ingest_pipeline, "generation_publication_state", _state)
    monkeypatch.setattr(ingest_pipeline, "_unpublish_generation", _unpublish)

    with pytest.raises(IngestPipelineError) as excinfo:
        await ingest_pipeline._refuse_raced_publication(
            session,
            job=_Job(),  # type: ignore[arg-type]
            document_id=_Job.document_id,
            version_id=VERSION_ID,
        )

    assert excinfo.value.code == FENCE_SUPERSEDED, "a visible reason code, not silence"
    assert excinfo.value.retryable is False, "a lost race is final, never retried"
    assert "replaced or deleted between the ready gate and the commit" in excinfo.value.message
    assert len(unpublished) == 1, "whatever the run wrote must be unpublished"


async def test_the_delete_case_refuses_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same loud refusal when the concurrent change was a delete."""
    session = _Session(rowcount=0)

    async def _state(*args, **kwargs):
        return FENCE_DELETED

    async def _unpublish(*args, **kwargs):
        return None

    monkeypatch.setattr(ingest_pipeline, "generation_publication_state", _state)
    monkeypatch.setattr(ingest_pipeline, "_unpublish_generation", _unpublish)

    with pytest.raises(IngestPipelineError) as excinfo:
        await ingest_pipeline._refuse_raced_publication(
            session,
            job=_Job(),  # type: ignore[arg-type]
            document_id=_Job.document_id,
            version_id=VERSION_ID,
        )

    assert excinfo.value.code == FENCE_DELETED


async def test_a_baffling_still_live_authority_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0 rows but the SELECT reads live ⇒ refuse anyway (fail closed, never guess)."""
    session = _Session(rowcount=0)

    async def _state(*args, **kwargs):
        return None  # inexplicably still live

    async def _unpublish(*args, **kwargs):
        return None

    monkeypatch.setattr(ingest_pipeline, "generation_publication_state", _state)
    monkeypatch.setattr(ingest_pipeline, "_unpublish_generation", _unpublish)

    with pytest.raises(IngestPipelineError) as excinfo:
        await ingest_pipeline._refuse_raced_publication(
            session,
            job=_Job(),  # type: ignore[arg-type]
            document_id=_Job.document_id,
            version_id=VERSION_ID,
        )

    assert excinfo.value.code == FENCE_SUPERSEDED, "refusing is the safe answer"
    assert excinfo.value.retryable is False


def test_the_pipeline_never_assigns_ready_through_the_orm_anymore() -> None:
    """The unpredicated ``job.status = READY`` is gone from the publish path.

    Any remaining assignment would re-open the race the CAS closes, so this
    pins the removal rather than trusting the review to notice.
    """
    import inspect

    source = inspect.getsource(ingest_pipeline.process_job)
    assert "job.status = JobStatus.READY.value" not in source, (
        "READY must only ever be written by the fenced UPDATE"
    )
    assert "publish_job_if_generation_live(" in source
    assert "_refuse_raced_publication(" in source
