"""T-V1-072: a superseded generation's vectors die durably.

T-V1-069 measured 66 points in the live collection whose version rows were
already superseded: ``supersede_other_versions`` deletes chunks+FTS in the same
transaction and then calls a **best-effort** vector cleanup whose only recourse
was a queued job retry that could never arrive. Two mechanisms close that here:

* the delete is retried in-process, bounded (``_VECTOR_CLEANUP_ATTEMPTS``), so a
  one-off store failure no longer outlives the job that caused it;
* ``reconcile_superseded_vectors`` sweeps every version row already marked
  superseded, so a cleanup that *was* lost is repaired on the next supersede.

Service-free: the store is replaced at the module the cleanup imports it from,
and the session is a double that answers the two version queries.
"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING, Any, cast

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion import dedup
from retrieval.adapters.qdrant_store import VectorStoreError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.unit


class _Scalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)


class _FakeStore:
    """Qdrant double: points per superseded version, with injectable failures."""

    def __init__(self) -> None:
        self.points: dict[str, list[str]] = {}
        self.attempts: list[list[str]] = []
        self.fail_times = 0
        self.closed = 0

    async def delete_versions(
        self, *, collection: str, version_ids: list[str], document_id: str | None = None
    ) -> int:
        self.attempts.append(list(version_ids))
        if len(self.attempts) <= self.fail_times:
            raise VectorStoreError("simulated qdrant outage during supersession cleanup")
        removed = 0
        for version_id in version_ids:
            removed += len(self.points.pop(version_id, []))
        return removed

    async def close(self) -> None:
        self.closed += 1


class _Job:
    def __init__(self, job_id: uuid.UUID) -> None:
        self.id = job_id
        self.attempts = 1
        self.max_attempts = 3
        self.error_code: str | None = None
        self.error_message: str | None = None


class _Version:
    def __init__(self, version_id: uuid.UUID, number: int) -> None:
        self.id = version_id
        self.document_id = DOCUMENT_ID
        self.version_number = number


class _Session:
    """Answers the two version queries the cleanup path makes."""

    def __init__(self, older: list[uuid.UUID], superseded: list[uuid.UUID], job: _Job) -> None:
        self.older = older
        self.superseded = superseded
        self.job = job
        self.statements: list[str] = []
        self.committed = 0

    async def execute(self, statement: Any) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        if "superseded_at IS NOT NULL" in sql:
            return _Result(list(self.superseded))
        if "version_number" in sql:  # strictly-older lookup
            return _Result(list(self.older))
        if "ingestion_jobs" in sql:  # latest_job_for_version (the marker path)
            return _Result([self.job] if self.job is not None else [])
        return _Result([])

    async def commit(self) -> None:
        self.committed += 1

    def db(self) -> AsyncSession:
        """The double as the product signature sees it (it is an AsyncSession)."""
        return cast("AsyncSession", self)

    def get(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("supersede path must not call session.get here")


DOCUMENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000072")
KEEP_ID = uuid.UUID("00000000-0000-0000-0000-000000000072")
V1 = uuid.UUID("00000000-0000-0000-0000-000000000071")


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    fake = _FakeStore()
    monkeypatch.setattr(
        "retrieval.adapters.qdrant_store.QdrantVectorStore",
        lambda *a, **k: fake,
    )
    monkeypatch.setattr(dedup.asyncio, "sleep", _no_sleep)
    return fake


async def _no_sleep(_seconds: float) -> None:
    return None


def _session(older: list[uuid.UUID], superseded: list[uuid.UUID] | None = None) -> _Session:
    return _Session(older, superseded if superseded is not None else list(older), _Job(KEEP_ID))


# --- the metric: one failed delete must still end clean --------------------


async def test_a_single_cleanup_failure_is_retried_and_leaves_no_orphan(store) -> None:
    """Fault injection: the vector delete fails once, and nothing survives.

    This is the ticket's metric. Before T-V1-072 this left the points behind and
    set a marker, relying on a queued job retry that could never arrive.
    """
    store.points[str(V1)] = ["p1", "p2"]
    store.fail_times = 1
    session = _session([V1])

    cleaned = await dedup._cleanup_superseded_vectors(
        session.db(), document_id=DOCUMENT_ID, version_ids=[V1], keep_version_id=KEEP_ID
    )

    assert cleaned is True
    assert len(store.attempts) == 2, "one failure, one retry — never a silent success"
    assert store.points == {}, "0 orphan points after a single failed cleanup"
    assert session.job.error_code is None, "a recovered cleanup must not leave a failure marker"


async def test_supersede_then_crash_leaves_no_orphans(store, monkeypatch) -> None:
    """End-to-end shape: supersede, cleanup fails every attempt, then reconcile.

    The durable half: the targeted cleanup raised and the DB half stayed
    pending, so the next reconcile (here: the one every supersede runs) removes
    the points that the lost cleanup left behind.
    """
    store.points[str(V1)] = ["p1"]
    store.fail_times = 99  # every attempt of the targeted cleanup fails
    session = _session([V1], superseded=[V1])

    # the marker + queued retry of T-V1-017 still fire (visible failure surface)
    assert (
        await dedup._cleanup_superseded_vectors(
            session.db(), document_id=DOCUMENT_ID, version_ids=[V1], keep_version_id=KEEP_ID
        )
        is False
    )
    assert session.job.error_code == "SUPERSESSION_CLEANUP_RETRY"
    assert store.points == {str(V1): ["p1"]}, "the orphan is still there — visibly pending"

    # store recovers; the reconcile sweep now finds the orphaned points
    store.fail_times = 0
    removed = await dedup.reconcile_superseded_vectors(session.db(), document_id=DOCUMENT_ID)

    assert removed == 1
    assert store.points == {}, "0 orphan points after the reconcile"


# --- the bounded budget ---------------------------------------------------


async def test_a_persistently_failing_cleanup_stays_loud(store) -> None:
    """Exhausting the budget keeps T-V1-017's marker + queued retry, never silent."""
    import ingestion.queue as queue_mod

    store.points[str(V1)] = ["p1"]  # the orphan a failed cleanup would leave
    store.fail_times = 99
    session = _session([V1])
    scheduled: list[tuple[str, float]] = []

    async def _capture(job_id: str, delay: float) -> None:
        scheduled.append((job_id, delay))

    original = queue_mod.schedule_retry
    queue_mod.schedule_retry = _capture  # type: ignore[assignment]
    try:
        assert (
            await dedup._cleanup_superseded_vectors(
                session.db(), document_id=DOCUMENT_ID, version_ids=[V1], keep_version_id=KEEP_ID
            )
            is False
        )
    finally:
        queue_mod.schedule_retry = original

    assert len(store.attempts) == dedup._VECTOR_CLEANUP_ATTEMPTS, "the retry budget is bounded"
    assert session.job.error_code == "SUPERSESSION_CLEANUP_RETRY"
    assert scheduled and scheduled[0][0] == str(KEEP_ID)
    assert store.points == {str(V1): ["p1"]}, "failure is visible, not reported as clean"


async def test_the_retry_budget_is_bounded() -> None:
    """A constant, not an open-ended retry-until-success."""
    assert dedup._VECTOR_CLEANUP_ATTEMPTS == 3
    assert dedup._VECTOR_CLEANUP_BACKOFF_SECONDS == (1.0, 2.0)


# --- reconcile semantics --------------------------------------------------


async def test_reconcile_only_touches_already_superseded_versions(store) -> None:
    """Postgres is the authority: nothing but ``superseded_at`` rows is swept."""
    session = _session([], superseded=[])

    removed = await dedup.reconcile_superseded_vectors(session.db(), document_id=DOCUMENT_ID)

    assert removed == 0
    assert store.attempts == [], "no superseded rows ⇒ no store call at all"


async def test_reconcile_is_scoped_to_one_document_when_asked(store) -> None:
    """A document-scoped sweep never passes another document's id downstream."""
    session = _session([], superseded=[V1])

    await dedup.reconcile_superseded_vectors(session.db(), document_id=DOCUMENT_ID)

    assert any("document_id" in sql for sql in session.statements)
    assert store.attempts == [[str(V1)]]
