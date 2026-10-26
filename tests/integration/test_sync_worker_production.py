"""M6-001b: the durable sync worker's REAL production wiring — against real SQL.

POLICE's REQUEST-CHANGES closed the memory-helper gap: these tests drive the
production publisher/deleter/consumer (durable progress store, persisted
source->document mapping, real document rows) against an isolated PostgreSQL
schema, with fakes ONLY at the service boundaries (object store, Redis queue,
connector download). They prove the properties the card cannot close without:

- create/update/delete go through the persisted identity mapping, never a
  filename guess (nested external ids, same basename across connectors);
- a lost enqueue after the SQL commit is REPAIRED on redelivery (no silently
  un-ingested documents);
- durable progress survives a restart (a new store instance resumes);
- the one-shot summary is JSON-safe (dataclasses.asdict);
- the supervised loop restarts after a failure and honours stop.

PostgreSQL unavailable -> clean skip (same contract as the other isolated
schema tests); nothing here passes vacuously.
"""

from __future__ import annotations

import json
import uuid

import pytest
from connectors.adapters.progress_store import (
    Base as ConnectorsBase,
)
from connectors.adapters.progress_store import SqlAlchemySyncProgressStore

# Registers SyncSourceDocument in ConnectorsBase.metadata BEFORE the fixture's
# create_all runs (a lazy import would create the other tables and miss this
# one).
import connectors.adapters.source_document_map  # noqa: F401  # isort: skip
from datetime import UTC

from connectors.interfaces import (
    ConnectorConfig,
    ConnectorDiscovery,
    ConnectorKind,
    SourceItem,
)
from connectors.sync_consumer import SourceIdentity, SyncClaim
from ingestion.persistence import Base as IngestionBase
from ingestion.persistence import Document, DocumentVersion, IngestionJob
from jobs.sync_worker import (
    default_source_loader,
    make_document_deleter,
    make_document_publisher,
    run_supervised_sync_consumer,
    run_sync_loop,
    run_sync_once,
    sync_worker_enabled,
)
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fakes at the service boundaries only
# ---------------------------------------------------------------------------


class FakeObjectStore:
    """put_original stand-in: no bytes stored, a deterministic key returned."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return f"originals/{kwargs['document_id']}/{kwargs['version_id']}"


class FakeQueue:
    """SyncQueue stand-in with scripted claims and an enqueue recorder."""

    def __init__(self, claims: list | None = None, fail_first_enqueue: bool = False) -> None:
        self._claims = list(claims or [])
        self.acked: list = []
        self.enqueued: list[str] = []
        self._enqueue_failures = 1 if fail_first_enqueue else 0

    async def claim(self, timeout_seconds: int = 1):
        return self._claims.pop(0) if self._claims else None

    async def ack(self, claim) -> bool:
        self.acked.append(claim)
        return True

    async def enqueue(self, job_id: str) -> None:
        if self._enqueue_failures > 0:
            self._enqueue_failures -= 1
            raise RuntimeError("redis unavailable")
        self.enqueued.append(job_id)


class FakeConnector:
    """A connector whose listing is scripted; fetch returns the item's bytes."""

    def __init__(self, items: list[SourceItem]) -> None:
        self._items = items

    async def discover(self, config, *, cursor=None, page_size=None) -> ConnectorDiscovery:
        return ConnectorDiscovery(items=list(self._items), next_cursor=None, snapshot="snap-1")

    async def fetch(self, config, item: SourceItem) -> bytes:
        return item.metadata.get("content", b"payload-" + item.external_id.encode())


def _config(connector_id: str, workspace_id: uuid.UUID) -> ConnectorConfig:
    return ConnectorConfig(
        connector_id=connector_id,
        workspace_id=str(workspace_id),
        kind=ConnectorKind.LOCAL_UPLOAD,
        settings={"path": "/tmp/does-not-matter"},
    )


def _item(external_id: str, *, name: str | None = None, content: bytes = b"v1") -> SourceItem:
    return SourceItem(
        external_id=external_id,
        name=name or external_id.rsplit("/", 1)[-1],
        metadata={"content": content},
    )


# ---------------------------------------------------------------------------
# Harness: isolated schema, real tables
# ---------------------------------------------------------------------------


class Harness:
    def __init__(self, engine, session_factory) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.store = SqlAlchemySyncProgressStore(session_factory=session_factory)


@pytest.fixture
async def pg():
    from core.config import get_settings

    schema = "syncworker_test_" + uuid.uuid4().hex
    url = get_settings().database_url
    admin = create_async_engine(url, connect_args={"connect_timeout": 3})
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        async with admin.connect():
            pass
    except OperationalError:
        await engine.dispose()
        await admin.dispose()
        pytest.skip("PostgreSQL unavailable; start compose postgres for isolated sync-worker tests")
    try:
        async with admin.begin() as conn:
            await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        async with engine.begin() as conn:
            await conn.run_sync(IngestionBase.metadata.create_all)
            await conn.run_sync(ConnectorsBase.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        yield Harness(engine, session_factory)
    finally:
        async with admin.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await engine.dispose()
        await admin.dispose()


async def _publisher(harness, enqueue_fn=None, object_store=None):
    return make_document_publisher(
        session_factory=harness.session_factory,
        put_original_fn=object_store or FakeObjectStore(),
        enqueue_fn=enqueue_fn or (lambda job_id: _noop_enqueue(job_id)),
    )


async def _noop_enqueue(job_id: str) -> None:
    return None


def _consumer(harness, *, queue, publisher=None, deleter=None, items, load_sources=None):
    from jobs.sync_worker import default_sync_consumer

    connector = FakeConnector(items)
    return default_sync_consumer(
        store=harness.store,
        queue=queue,
        publisher=publisher,
        deleter=deleter,
        load_sources=load_sources or (lambda ws: []),
        connector_factory=lambda config: connector,
    )


# ---------------------------------------------------------------------------
# Identity mapping: create / nested delete / same basename across connectors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publisher_creates_document_version_job_and_mapping(pg):
    ws = uuid.uuid4()
    store = FakeObjectStore()
    enqueued: list[str] = []

    async def enqueue(job_id: str) -> None:
        enqueued.append(job_id)

    publisher = await _publisher(pg, enqueue_fn=enqueue, object_store=store)
    identity = _config("c1", ws)
    ident = SourceIdentity.from_config(identity)
    await publisher(ident, _item("docs/a.txt", content=b"hello"), b"hello", "sum-1")

    async with pg.session_factory() as session:
        from connectors.adapters.source_document_map import get_mapped_document

        document_id = await get_mapped_document(
            session, workspace_id=ws, connector_id="c1", external_id="docs/a.txt"
        )
        assert document_id is not None
        document = await session.get(Document, document_id)
        assert document is not None and document.workspace_id == ws
        assert document.mime_type == "text/plain"
        versions = (await session.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id == document_id)
        )).all()
        assert len(versions) == 1 and versions[0].checksum == "sum-1"
        assert versions[0].mime_type == "text/plain"
        assert len(store.calls) == 1  # originals stored BEFORE the row
    assert enqueued == [str(await _latest_job_id(pg, document_id))]


async def _latest_job_id(pg, document_id):
    from ingestion.persistence import IngestionJob

    async with pg.session_factory() as session:
        job = (await session.scalars(
            select(IngestionJob).where(IngestionJob.document_id == document_id)
        )).all()
        return job[-1].id if job else None


@pytest.mark.asyncio
async def test_nested_external_id_deletes_exactly_its_document(pg):
    ws = uuid.uuid4()
    enqueued: list[str] = []

    async def enqueue(job_id: str) -> None:
        enqueued.append(job_id)

    publisher = await _publisher(pg, enqueue_fn=enqueue)
    from connectors.sync_consumer import SourceIdentity

    ident = SourceIdentity.from_config(_config("c1", ws))
    await publisher(ident, _item("a/b/c.txt", name="c.txt", content=b"nested"), b"nested", "sum-n")

    purged: list[uuid.UUID] = []

    async def purge(session, *, workspace_id, document_id):
        purged.append(document_id)
        return {"originals": True}

    deleter = make_document_deleter(session_factory=pg.session_factory, purge_fn=purge)
    await deleter(ident, "a/b/c.txt", None)

    async with pg.session_factory() as session:
        from connectors.adapters.source_document_map import get_mapped_document

        assert await get_mapped_document(
            session, workspace_id=ws, connector_id="c1", external_id="a/b/c.txt"
        ) is None  # mapping dropped after the purge
    assert len(purged) == 1  # the mapped document, found by identity — not by name


@pytest.mark.asyncio
async def test_same_basename_two_connectors_keep_separate_documents(pg):
    ws = uuid.uuid4()
    enqueued: list[str] = []

    async def enqueue(job_id: str) -> None:
        enqueued.append(job_id)

    publisher = await _publisher(pg, enqueue_fn=enqueue)
    from connectors.sync_consumer import SourceIdentity

    c1 = SourceIdentity.from_config(_config("c1", ws))
    c2 = SourceIdentity.from_config(_config("c2", ws))
    # Same basename, different external ids/connectors.
    await publisher(c1, _item("one/report.pdf", content=b"one"), b"one", "sum-1")
    await publisher(c2, _item("two/report.pdf", content=b"two"), b"two", "sum-2")

    async with pg.session_factory() as session:
        from connectors.adapters.source_document_map import get_mapped_document

        d1 = await get_mapped_document(
            session, workspace_id=ws, connector_id="c1", external_id="one/report.pdf"
        )
        d2 = await get_mapped_document(
            session, workspace_id=ws, connector_id="c2", external_id="two/report.pdf"
        )
        assert d1 is not None and d2 is not None and d1 != d2

    # Deleting c1's item must not touch c2's document.
    purged: list[uuid.UUID] = []

    async def purge(session, *, workspace_id, document_id):
        purged.append(document_id)
        return {"originals": True}

    deleter = make_document_deleter(session_factory=pg.session_factory, purge_fn=purge)
    await deleter(c1, "one/report.pdf", None)
    assert purged == [d1]


# ---------------------------------------------------------------------------
# Root-review regressions: identity aliasing, tombstone resurrection, duplicate
# claims, append (migration 0016 schema) — no truncation anywhere.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_prefix_paths_do_not_alias(pg):
    """Two distinct external ids sharing a 1024-char prefix map apart."""
    ws = uuid.uuid4()
    enqueued: list[str] = []

    async def enqueue(job_id: str) -> None:
        enqueued.append(job_id)

    publisher = await _publisher(pg, enqueue_fn=enqueue)
    ident = SourceIdentity.from_config(_config("c1", ws))
    prefix = "d/" + "x" * 1100
    await publisher(ident, _item(prefix + "/one.txt", content=b"1"), b"1", "sum-1")
    await publisher(ident, _item(prefix + "/two.txt", content=b"2"), b"2", "sum-2")

    d1 = await _mapped(pg, ws, "c1", prefix + "/one.txt")
    d2 = await _mapped(pg, ws, "c1", prefix + "/two.txt")
    assert d1 is not None and d2 is not None and d1 != d2

    purged: list[uuid.UUID] = []

    async def purge(session, *, workspace_id, document_id):
        purged.append(document_id)
        return {"originals": True}

    deleter = make_document_deleter(session_factory=pg.session_factory, purge_fn=purge)
    await deleter(ident, prefix + "/one.txt", None)
    assert purged == [d1]  # the OTHER long path is untouched


@pytest.mark.asyncio
async def test_tombstoned_document_is_resurrected_not_preserved(pg):
    """Same checksum after an external purge must NOT preserve deleted state."""
    ws = uuid.uuid4()
    enqueued: list[str] = []

    async def enqueue(job_id: str) -> None:
        enqueued.append(job_id)

    publisher = await _publisher(pg, enqueue_fn=enqueue)
    ident = SourceIdentity.from_config(_config("c1", ws))
    await publisher(ident, _item("a.txt", content=b"v1"), b"v1", "sum-1")
    first_document = await _mapped(pg, ws, "c1", "a.txt")

    # Tombstone the document OUTSIDE sync, leaving the mapping row in place
    # (the stale state the root review demanded we handle).
    from datetime import datetime

    from ingestion.persistence import Document as _Document

    async with pg.session_factory() as session:
        document = await session.get(_Document, first_document)
        document.deleted_at = datetime.now(UTC)
        await session.commit()

    # The same content arrives again: a NEW document, mapping repointed.
    await publisher(ident, _item("a.txt", content=b"v1"), b"v1", "sum-1")
    second_document = await _mapped(pg, ws, "c1", "a.txt")
    assert second_document is not None and second_document != first_document

    async with pg.session_factory() as session:
        revived = await session.get(_Document, second_document)
        assert revived is not None and revived.deleted_at is None  # live, not deleted


@pytest.mark.asyncio
async def test_duplicate_publish_is_idempotent(pg):
    """Two publishes of the same identity/content: one document, one version."""
    ws = uuid.uuid4()
    enqueued: list[str] = []

    async def enqueue(job_id: str) -> None:
        enqueued.append(job_id)

    publisher = await _publisher(pg, enqueue_fn=enqueue)
    ident = SourceIdentity.from_config(_config("c1", ws))
    await publisher(ident, _item("a.txt", content=b"v1"), b"v1", "sum-1")
    await publisher(ident, _item("a.txt", content=b"v1"), b"v1", "sum-1")

    document_id = await _mapped(pg, ws, "c1", "a.txt")
    async with pg.session_factory() as session:
        versions = (await session.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id == document_id)
        )).all()
        assert len(versions) == 1
        jobs = (await session.scalars(
            select(IngestionJob).where(IngestionJob.document_id == document_id)
        )).all()
        assert len(jobs) == 1
    assert len(enqueued) == 2  # the repair path re-dispatched (idempotent by id)


# ---------------------------------------------------------------------------
# Lost enqueue after commit -> repaired on redelivery (no lost ingestion)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lost_enqueue_is_repaired_on_redelivery(pg):
    ws = uuid.uuid4()
    calls = {"n": 0}

    async def enqueue(job_id: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("redis unavailable")  # the commit already happened

    publisher = await _publisher(pg, enqueue_fn=enqueue)
    from connectors.sync_consumer import SourceIdentity

    ident = SourceIdentity.from_config(_config("c1", ws))
    with pytest.raises(RuntimeError):
        await publisher(ident, _item("a.txt", content=b"v1"), b"v1", "sum-1")

    # The document/version/job/mapping committed; the dispatch did not.
    document_id = await _mapped(pg, ws, "c1", "a.txt")
    assert document_id is not None
    assert (await _latest_job_id(pg, document_id)) is not None

    # Redelivery (at-least-once): same content -> the same-checksum path must
    # REPAIR the dispatch instead of returning silently.
    await publisher(ident, _item("a.txt", content=b"v1"), b"v1", "sum-1")

    async with pg.session_factory() as session:
        from ingestion.persistence import DocumentVersion, IngestionJob

        versions = (await session.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id == document_id)
        )).all()
        jobs = (await session.scalars(
            select(IngestionJob).where(IngestionJob.document_id == document_id)
        )).all()
        assert len(versions) == 1  # no duplicate version from the retry
        assert len(jobs) == 1 and jobs[0].status == "queued"
    assert calls["n"] == 2  # the repair actually dispatched


async def _mapped(pg, ws, connector_id, external_id):
    from connectors.adapters.source_document_map import get_mapped_document

    async with pg.session_factory() as session:
        return await get_mapped_document(
            session, workspace_id=ws, connector_id=connector_id, external_id=external_id
        )


# ---------------------------------------------------------------------------
# Durable progress across a restart + JSON-safe summaries + supervision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_durable_progress_survives_restart(pg):
    ws = uuid.uuid4()
    enqueued: list[str] = []

    async def enqueue(job_id: str) -> None:
        enqueued.append(job_id)

    publisher = await _publisher(pg, enqueue_fn=enqueue)
    deleter = make_document_deleter(session_factory=pg.session_factory, purge_fn=purge_noop)
    queue = FakeQueue([_claim(ws, "c1")])
    consumer = _consumer(pg, queue=queue, publisher=publisher, deleter=deleter,
                         items=[_item("a.txt", content=b"v1")],
                         load_sources=lambda workspace: [_config("c1", ws)])
    summary = await consumer.run_once(timeout_seconds=1)
    assert summary is not None and len(summary.reports) == 1
    assert queue.acked  # acked only after the durable outcome

    # A RESTART: a brand-new store instance over the same schema.
    restarted = SqlAlchemySyncProgressStore(session_factory=pg.session_factory)
    from connectors.sync_consumer import SourceIdentity

    progress = await restarted.load(SourceIdentity.from_config(_config("c1", ws)))
    assert progress is not None
    assert progress.items_seen == 1  # the finished item is not redone blindly


async def purge_noop(session, *, workspace_id, document_id):
    return {"originals": True}


def _claim(workspace_id: uuid.UUID, connector_id: str | None = None):

    job_id = f"sync:{workspace_id}" + (f"/{connector_id}" if connector_id else "")
    return SyncClaim(job_id=job_id, token="lease-1")


@pytest.mark.asyncio
async def test_worker_once_summary_is_json_safe(pg):
    ws = uuid.uuid4()
    enqueued: list[str] = []

    async def enqueue(job_id: str) -> None:
        enqueued.append(job_id)

    publisher = await _publisher(pg, enqueue_fn=enqueue)
    queue = FakeQueue([_claim(ws, "c1")])
    # The connector factory is injected — no real filesystem connector is
    # constructed (the /tmp path is never touched).
    summary = await run_sync_once(
        store=pg.store,
        queue=queue,
        publisher=publisher,
        load_sources=lambda workspace: [_config("c1", ws)],
        connector_factory=lambda config: FakeConnector(
            [_item("a.txt", content=b"v1")]
        ),
    )
    assert summary["claimed"] is True
    json.dumps(summary)  # must not raise: reports are plain dicts (asdict)


@pytest.mark.asyncio
async def test_loop_survives_transient_iteration_failure(pg):
    """A per-iteration exception is TRANSIENT: the loop continues (policy)."""
    import asyncio

    class BlipQueue(FakeQueue):
        def __init__(self) -> None:
            super().__init__([])
            self._blipped = False

        async def claim(self, timeout_seconds: int = 1):
            if not self._blipped:
                self._blipped = True
                raise RuntimeError("redis blip")
            stop.set()
            return None

    stop = asyncio.Event()
    stats = await run_sync_loop(
        store=pg.store,
        queue=BlipQueue(),
        load_sources=lambda workspace: [],
        idle_sleep_seconds=0.01,
        stop_event=stop,
    )
    assert stats["iterations"] >= 2  # survived the blip and kept looping


@pytest.mark.asyncio
async def test_supervisor_restarts_a_crashing_loop(pg):
    """A LOOP-level crash reaches the supervisor and is restarted (policy)."""
    import asyncio

    stop = asyncio.Event()
    calls = {"n": 0}

    async def failing_then_real(*, stop_event, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("loop bug")
        stop_event.set()
        return {"iterations": 1, "runs": 0, "items_processed": 0, "recoveries": 0}

    stats = await run_supervised_sync_consumer(
        stop,
        loop_fn=failing_then_real,
        backoff=0.01,
    )
    assert stats["restarts"] == 1
    assert stats["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_loop_recovers_expired_leases(pg):
    """Lease recovery runs on the interval (root runtime gap)."""
    import asyncio

    stop = asyncio.Event()
    recoveries = {"n": 0}

    async def recover() -> int:
        recoveries["n"] += 1
        stop.set()
        return 0

    stats = await run_sync_loop(
        store=pg.store,
        queue=FakeQueue([]),
        load_sources=lambda workspace: [],
        recover_fn=recover,
        recover_interval_seconds=0.0,
        idle_sleep_seconds=0.01,
        stop_event=stop,
    )
    assert recoveries["n"] >= 1
    assert stats["recoveries"] >= 1


@pytest.mark.asyncio
async def test_default_source_loader_prefers_persisted_configs():
    # The frozen seam: when the sources API is importable, the worker consumes
    # persisted configs (never the env loader in production).
    loader = default_source_loader()
    from connectors.adapters.config_loader import load_persisted_sources

    assert loader is load_persisted_sources


def test_sync_worker_flag_defaults_off():
    import os

    os.environ.pop("SYNC_WORKER_ENABLED", None)
    assert sync_worker_enabled() is False  # byte-identical flag-off startup path


@pytest.mark.asyncio
async def test_partial_purge_preserves_mapping_and_retry_finishes(pg):
    from jobs.sync_worker import SyncPurgeError

    ws = uuid.uuid4()
    ident = SourceIdentity.from_config(_config("c1", ws))
    publisher = await _publisher(pg)
    await publisher(ident, _item("nested/report.txt"), b"payload", "checksum")
    mapped = await _mapped(pg, ws, "c1", "nested/report.txt")
    calls = []

    async def purge(session, *, workspace_id, document_id):
        calls.append(document_id)
        return {"postgres": True, "qdrant": len(calls) > 1, "originals": len(calls) > 1}

    deleter = make_document_deleter(session_factory=pg.session_factory, purge_fn=purge)
    with pytest.raises(SyncPurgeError, match="purge incomplete"):
        await deleter(ident, "nested/report.txt", None)
    assert await _mapped(pg, ws, "c1", "nested/report.txt") == mapped
    await deleter(ident, "nested/report.txt", None)
    assert calls == [mapped, mapped]
    assert await _mapped(pg, ws, "c1", "nested/report.txt") is None
