"""T-V1-034 — sync consumer with persisted cursors.

Locks the ticket's two metrics and the persistence contract:

- **positional-rescan skips: 0** — progress is a keyset cursor; deleting the
  item the cursor points at (or inserting before it) must not skip or duplicate
  anything, and the legacy *offset* cursor is shown to do exactly that;
- **progress durable across restarts** — a second consumer on the same store
  state resumes where the first died, publishes each changed item exactly once
  and acks only after the durable write;
- workspace configuration, source identity (settings fingerprint → generation),
  snapshot drift disclosure, item outcomes with T-V1-025 canonical codes.

No network, no services: an in-memory store, a fake queue and the real
``LocalFolderConnector`` on a tmp_path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from connectors import ConnectorConfig, ConnectorKind, create_connector
from connectors.interfaces import (
    Connector,
    ConnectorDiscovery,
    SourceItem,
    keyset_cursor,
)
from connectors.sync_consumer import (
    WORKSPACE_CONNECTOR_CONFIG_ENV,
    InMemorySyncProgressStore,
    ItemStatus,
    QueuedSyncConsumer,
    SourceIdentity,
    SyncClaim,
    SyncProgress,
    SyncReport,
    encode_sync_job,
    load_workspace_sources,
    parse_sync_job,
    settings_fingerprint,
)
from core.errors import ReasonCode, canonical_code, normalize_code

pytestmark = pytest.mark.unit


# ---- helpers -----------------------------------------------------------------


class FakeQueue:
    """Scripted :class:`SyncQueue`: pops payloads, records acks, never fails."""

    def __init__(self, payloads: list[str], events: list[str] | None = None) -> None:
        self._payloads = list(payloads)
        self.claimed: list[SyncClaim] = []
        self.acked: list[SyncClaim] = []
        #: shared with the store so ordering assertions see ONE real timeline
        self.events: list[str] = events if events is not None else []

    async def claim(self, timeout_seconds: int = 1) -> SyncClaim | None:
        if not self._payloads:
            return None
        claim = SyncClaim(job_id=self._payloads.pop(0), token="tok")
        self.claimed.append(claim)
        self.events.append("claim")
        return claim

    async def ack(self, claim: SyncClaim) -> bool:
        self.acked.append(claim)
        self.events.append("ack")
        return True


class RecordingStore(InMemorySyncProgressStore):
    """In-memory store that logs when the durable writes happen."""

    def __init__(
        self, state: dict[str, dict[str, Any]] | None = None, events: list[str] | None = None
    ) -> None:
        super().__init__(state)
        self.events: list[str] = events if events is not None else []

    async def save(self, progress: SyncProgress) -> None:
        self.events.append("save")
        await super().save(progress)

    async def record_outcome(self, identity: SourceIdentity, outcome) -> None:
        self.events.append("outcome")
        await super().record_outcome(identity, outcome)


class RecordingPublisher:
    """Captures published (created/updated) items; optionally fails on one id."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.published: list[str] = []
        self.fail_on = fail_on

    async def __call__(self, identity, item, content: bytes, checksum: str) -> None:
        if item.external_id == self.fail_on:
            raise RuntimeError("publisher exploded")
        self.published.append(item.external_id)
        assert checksum == hashlib.sha256(content).hexdigest()


def _folder_config(tmp_path: Path, connector_id: str = "c1", **settings: Any) -> ConnectorConfig:
    return ConnectorConfig(
        connector_id=connector_id,
        workspace_id="ws-a",
        kind=ConnectorKind.LOCAL_UPLOAD,
        settings={"path": str(tmp_path), **settings},
    )


def _write(tmp_path: Path, name: str, body: str) -> None:
    (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text(body, encoding="utf-8")


class CountingFolderConnector:
    """Real LocalFolderConnector that counts fetches (for the skip assertions)."""

    def __init__(self, inner: Connector) -> None:
        self._inner = inner
        self.fetched: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def kind(self) -> ConnectorKind:
        return self._inner.kind

    async def discover(self, config, cursor=None) -> ConnectorDiscovery:
        return await self._inner.discover(config, cursor=cursor)

    async def sync(self, config, cursor=None):
        return await self._inner.sync(config, cursor=cursor)

    async def fetch(self, config, item: SourceItem) -> bytes:
        self.fetched.append(item.external_id)
        return await self._inner.fetch(config, item)


def _consumer(
    store: InMemorySyncProgressStore,
    queue: FakeQueue,
    *,
    publisher=None,
    page_size: int = 100,
    load_sources=None,
    connector_factory=None,
) -> QueuedSyncConsumer:
    kwargs: dict[str, Any] = {
        "store": store,
        "queue": queue,
        "page_size": page_size,
        "publisher": publisher,
    }
    if load_sources is not None:
        kwargs["load_sources"] = load_sources
    if connector_factory is not None:
        kwargs["connector_factory"] = connector_factory
    return QueuedSyncConsumer(**kwargs)


# ---- metrics: no positional rescan, progress survives restart ----------------


async def test_first_run_creates_everything_and_clears_the_cursor(tmp_path: Path) -> None:
    for name in ("a.md", "b.md", "c.md"):
        _write(tmp_path, name, f"body {name}")
    config = _folder_config(tmp_path)
    store = InMemorySyncProgressStore()
    queue = FakeQueue([encode_sync_job("ws-a")])
    publisher = RecordingPublisher()
    consumer = _consumer(
        store, queue, publisher=publisher, page_size=1, load_sources=lambda _ws: [config]
    )

    summary = await consumer.run_once()

    assert summary is not None and summary.acked
    (report,) = summary.reports
    assert isinstance(report, SyncReport)
    assert (report.discovered, report.created, report.failed) == (3, 3, 0)
    assert report.pages == 3
    assert report.resumed is False
    assert report.cursor is None  # run completed → next run starts a fresh listing
    assert sorted(publisher.published) == ["a.md", "b.md", "c.md"]

    progress = await store.load(SourceIdentity.from_config(config))
    assert progress is not None
    assert progress.cursor is None  # persisted progress: complete, not offset-based
    assert progress.items_created == 3
    outcomes = await store.outcomes(progress.identity, progress.generation)
    assert set(outcomes) == {"a.md", "b.md", "c.md"}
    assert all(o.status is ItemStatus.CREATED for o in outcomes.values())
    assert all(o.checksum and len(o.checksum) == 64 for o in outcomes.values())


async def test_deleting_the_cursor_item_does_not_skip_the_next_one(tmp_path: Path) -> None:
    """THE metric: keyset progress ⇒ 0 positional-rescan skips.

    ``a.md`` is the persisted cursor. It disappears before the resume (the
    classic case where an offset cursor jumps over the item after it), and
    ``b.md``/``c.md`` must both still be processed.
    """
    for name in ("a.md", "b.md", "c.md"):
        _write(tmp_path, name, f"body {name}")
    store = InMemorySyncProgressStore()
    queue = FakeQueue([])
    # Publisher explodes on page 2 → run_sync raises after page 1 was saved.
    consumer = _consumer(store, queue, publisher=RecordingPublisher(fail_on="b.md"), page_size=1)

    with pytest.raises(RuntimeError, match="publisher exploded"):
        await consumer.run_sync(_folder_config(tmp_path))

    mid = await store.load(SourceIdentity.from_config(_folder_config(tmp_path)))
    assert mid is not None and mid.cursor == keyset_cursor("a.md")

    (tmp_path / "a.md").unlink()  # the cursor item itself is gone

    resumed = _consumer(store, queue, publisher=RecordingPublisher(), page_size=1)
    report = await resumed.run_sync(_folder_config(tmp_path))

    assert report.resumed is True
    outcomes = await store.outcomes(
        SourceIdentity.from_config(_folder_config(tmp_path)), report.generation
    )
    # b and c both processed — neither skipped nor lost behind the dead cursor.
    assert outcomes["b.md"].status is ItemStatus.CREATED
    assert outcomes["c.md"].status is ItemStatus.CREATED
    assert (report.created, report.failed) == (2, 0)


async def test_offset_cursor_which_the_consumer_never_persists_would_skip(
    tmp_path: Path,
) -> None:
    """Contrast control for the metric above (documents why offsets are banned)."""
    from connectors.adapters import LocalFolderConnector

    for name in ("a.md", "b.md", "c.md"):
        _write(tmp_path, name, "x")
    (tmp_path / "a.md").unlink()
    connector = LocalFolderConnector(page_size=10)
    legacy = await connector.discover(_folder_config(tmp_path), cursor="1")
    assert [i.external_id for i in legacy.items] == ["c.md"]  # b.md skipped by offset


async def test_insert_before_the_cursor_neither_duplicates_nor_skips(
    tmp_path: Path,
) -> None:
    for name in ("b.md", "c.md", "d.md"):
        _write(tmp_path, name, "x")
    store = InMemorySyncProgressStore()
    queue = FakeQueue([])
    consumer = _consumer(store, queue, publisher=RecordingPublisher(fail_on="c.md"), page_size=1)

    with pytest.raises(RuntimeError):
        await consumer.run_sync(_folder_config(tmp_path))
    mid = await store.load(SourceIdentity.from_config(_folder_config(tmp_path)))
    assert mid is not None and mid.cursor == keyset_cursor("b.md")

    _write(tmp_path, "0.md", "brand new, sorts before the cursor")  # insert before cursor

    publisher = RecordingPublisher()
    resumed = _consumer(store, queue, publisher=publisher, page_size=1)
    report = await resumed.run_sync(_folder_config(tmp_path))

    # 0.md sorts BEFORE the cursor, so this run does not re-serve it — and it
    # is not lost: the completed run clears the cursor, so the next run's fresh
    # listing picks it up.
    assert report.discovered == 2
    assert sorted(publisher.published) == ["c.md", "d.md"]
    assert report.cursor is None

    next_run = await _consumer(store, queue).run_sync(_folder_config(tmp_path))
    assert next_run.discovered == 4  # 0.md, b.md, c.md, d.md — full fresh listing
    assert next_run.created == 1  # the pre-cursor insert is finally seen
    assert next_run.unchanged == 3  # b + c + d already known by checksum


async def test_restart_resumes_from_the_store_and_publishes_each_item_once(
    tmp_path: Path,
) -> None:
    for name in ("a.md", "b.md", "c.md"):
        _write(tmp_path, name, "x")
    state: dict[str, dict[str, Any]] = {}
    queue = FakeQueue([])

    first_publisher = RecordingPublisher(fail_on="c.md")
    crashed = _consumer(RecordingStore(state), queue, publisher=first_publisher, page_size=1)
    with pytest.raises(RuntimeError):
        await crashed.run_sync(_folder_config(tmp_path))

    # "Restart": a brand-new consumer + store object over the same persisted state.
    second_publisher = RecordingPublisher()
    survivor = _consumer(RecordingStore(state), queue, publisher=second_publisher, page_size=1)
    report = await survivor.run_sync(_folder_config(tmp_path))

    assert report.resumed is True
    assert report.cursor is None
    assert first_publisher.published == ["a.md", "b.md"]  # done before the crash
    assert second_publisher.published == ["c.md"]  # resumed, no duplicates, no redo
    assert report.created == 1 and report.unchanged == 0


async def test_unchanged_items_are_not_fetched_again(tmp_path: Path) -> None:
    for name in ("a.md", "b.md"):
        _write(tmp_path, name, "same")
    store = InMemorySyncProgressStore()
    queue = FakeQueue([])

    await _consumer(store, queue).run_sync(_folder_config(tmp_path))

    inner = CountingFolderConnector(create_connector(_folder_config(tmp_path)))
    report = await _consumer(store, queue, connector_factory=lambda _cfg: inner).run_sync(
        _folder_config(tmp_path)
    )
    assert report.unchanged == 2
    assert inner.fetched == []  # checksums came from the adapter's own scan

    _write(tmp_path, "b.md", "changed body")
    inner2 = CountingFolderConnector(create_connector(_folder_config(tmp_path)))
    report2 = await _consumer(store, queue, connector_factory=lambda _cfg: inner2).run_sync(
        _folder_config(tmp_path)
    )
    assert (report2.unchanged, report2.updated, report2.created) == (1, 1, 0)
    assert inner2.fetched == ["b.md"]  # only the changed item is re-downloaded


async def test_failed_item_is_visible_with_a_canonical_code_and_the_run_continues(
    tmp_path: Path,
) -> None:
    for name in ("a.md", "b.md"):
        _write(tmp_path, name, "x")

    from connectors.adapters import LocalFolderConnector

    class OneBadApple(LocalFolderConnector):
        async def fetch(self, config, item: SourceItem) -> bytes:
            if item.external_id == "a.md":
                raise FileNotFoundError("gone")
            return await super().fetch(config, item)

    store = InMemorySyncProgressStore()
    consumer = _consumer(store, FakeQueue([]), connector_factory=lambda _cfg: OneBadApple())
    report = await consumer.run_sync(_folder_config(tmp_path))

    assert (report.failed, report.created) == (1, 1)
    assert report.errors == ["a.md: ingestion_error"]
    outcomes = await store.outcomes(
        SourceIdentity.from_config(_folder_config(tmp_path)), report.generation
    )
    assert outcomes["a.md"].status is ItemStatus.FAILED
    # T-V1-025: stored as the wire value of a CANONICAL registry code.
    assert outcomes["a.md"].error_code == normalize_code(ReasonCode.INGESTION_ERROR)
    assert canonical_code(outcomes["a.md"].error_code) is ReasonCode.INGESTION_ERROR
    assert outcomes["b.md"].status is ItemStatus.CREATED


async def test_snapshot_drift_is_disclosed_not_hidden(tmp_path: Path) -> None:
    """A listing that changes mid-run is reported; the keyset still advances."""
    _write(tmp_path, "a.md", "x")
    _write(tmp_path, "b.md", "x")

    from connectors.adapters import LocalFolderConnector

    class Drift(LocalFolderConnector):
        async def discover(self, config, cursor=None) -> ConnectorDiscovery:
            disc = await super().discover(config, cursor=cursor)
            if cursor is None:
                return disc  # page 1 captures the real listing digest
            # ...and the source churns before page 2 is fetched.
            return disc.model_copy(update={"snapshot": "0" * 64})

    store = InMemorySyncProgressStore()
    consumer = _consumer(
        store,
        FakeQueue([]),
        connector_factory=lambda _cfg: Drift(page_size=1),
        page_size=1,
    )
    report = await consumer.run_sync(_folder_config(tmp_path))
    assert report.snapshot_drift is True
    assert report.discovered == 2


# ---- queued consumption: ack only after the durable outcome ------------------


async def test_ack_happens_after_the_durable_write(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "x")
    config = _folder_config(tmp_path)
    events: list[str] = []
    store = RecordingStore(events=events)
    queue = FakeQueue([encode_sync_job("ws-a")], events=events)
    summary = await _consumer(store, queue, load_sources=lambda _ws: [config]).run_once()

    assert summary is not None and summary.acked
    assert "save" in events and "ack" in events
    # one real timeline: every durable write happens BEFORE the claim is
    # released, and the ack is the final step of the job.
    assert events[-1] == "ack"
    assert events.index("ack") > max(i for i, e in enumerate(events) if e == "save")


async def test_failure_leaves_the_claim_unacked_for_lease_redelivery(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "a.md", "x")
    config = _folder_config(tmp_path)
    store = InMemorySyncProgressStore()
    queue = FakeQueue([encode_sync_job("ws-a")])
    consumer = _consumer(
        store,
        queue,
        publisher=RecordingPublisher(fail_on="a.md"),
        load_sources=lambda _ws: [config],
    )

    with pytest.raises(RuntimeError, match="publisher exploded"):
        await consumer.run_once()

    assert queue.acked == []  # T-V1-020: lease expires → redelivered → resumes


async def test_run_once_with_no_work_returns_none() -> None:
    summary = await _consumer(InMemorySyncProgressStore(), FakeQueue([])).run_once()
    assert summary is None


async def test_sync_job_payload_codec_rejects_foreign_payloads() -> None:
    assert parse_sync_job("sync:ws-a") == ("ws-a", None)
    assert parse_sync_job("sync:ws-a/docs") == ("ws-a", "docs")
    for bad in ("ingest:42", "sync:", "sync:ws-a/", "not-a-payload"):
        with pytest.raises(ValueError):
            parse_sync_job(bad)
    assert encode_sync_job("ws-a", "docs") == "sync:ws-a/docs"
    with pytest.raises(ValueError):
        encode_sync_job("")


# ---- source identity, workspace configuration -------------------------------


async def test_reconfigured_source_bumps_generation_and_drops_old_checksums(
    tmp_path: Path,
) -> None:
    for name in ("a.md", "b.md"):
        _write(tmp_path, name, "x")
    store = InMemorySyncProgressStore()
    queue = FakeQueue([])
    await _consumer(store, queue).run_sync(_folder_config(tmp_path))

    changed = _folder_config(tmp_path, page_hint="by-name")  # settings changed
    report = await _consumer(store, queue).run_sync(changed)

    assert report.generation == 2
    assert report.created == 2  # old checksums must not decide "unchanged"
    progress = await store.load(SourceIdentity.from_config(changed))
    assert progress is not None and progress.generation == 2
    assert progress.cursor is None


async def test_settings_fingerprint_is_order_independent_and_content_sensitive() -> None:
    assert settings_fingerprint({"a": 1, "b": 2}) == settings_fingerprint({"b": 2, "a": 1})
    assert settings_fingerprint({"a": 1}) != settings_fingerprint({"a": 2})


def test_load_workspace_sources_parses_the_env_store() -> None:
    env = {
        WORKSPACE_CONNECTOR_CONFIG_ENV: json.dumps(
            {
                "ws-a": [
                    {
                        "connector_id": "docs",
                        "kind": "s3",
                        "settings": {"bucket": "b", "prefix": "docs/"},
                    },
                    {"connector_id": "site", "kind": "web", "settings": {"urls": []}},
                ],
                "ws-b": {"connector_id": "one", "kind": "local_upload", "settings": {}},
            }
        )
    }
    configs = load_workspace_sources("ws-a", env=env)
    assert [c.connector_id for c in configs] == ["docs", "site"]
    assert configs[0].kind is ConnectorKind.S3
    assert configs[0].workspace_id == "ws-a"
    # single-object shorthand for another workspace
    single = load_workspace_sources("ws-b", env=env)
    assert [c.connector_id for c in single] == ["one"]
    assert load_workspace_sources("ws-none", env=env) == []


def test_load_workspace_sources_skips_bad_entries_loudly() -> None:
    env = {
        WORKSPACE_CONNECTOR_CONFIG_ENV: json.dumps(
            {
                "ws-a": [
                    {"connector_id": "ok", "kind": "local_upload"},
                    {"connector_id": "nope", "kind": "not_a_kind"},
                    {"kind": "s3"},
                    "not-a-dict",
                ]
            }
        )
    }
    configs = load_workspace_sources("ws-a", env=env)
    assert [c.connector_id for c in configs] == ["ok"]

    assert load_workspace_sources("ws-a", env={WORKSPACE_CONNECTOR_CONFIG_ENV: "{oops"}) == []
    assert (
        load_workspace_sources(
            "ws-a", env={WORKSPACE_CONNECTOR_CONFIG_ENV: json.dumps(["list"])}
        )
        == []
    )
    assert load_workspace_sources("ws-a", env={}) == []


async def test_run_once_syncs_only_the_requested_connector(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "x")
    store = InMemorySyncProgressStore()
    queue = FakeQueue([encode_sync_job("ws-a", "wanted")])
    loads = {
        "wanted": _folder_config(tmp_path, connector_id="wanted"),
        "other": _folder_config(tmp_path, connector_id="other"),
    }
    summary = await _consumer(store, queue, load_sources=lambda ws: list(loads.values())).run_once()

    assert summary is not None and summary.connector_id == "wanted"
    assert [r.connector_id for r in summary.reports] == ["wanted"]


async def test_run_once_for_an_unconfigured_connector_is_a_no_op_not_a_loop() -> None:
    store = InMemorySyncProgressStore()
    queue = FakeQueue([encode_sync_job("ws-a", "ghost")])
    summary = await _consumer(store, queue, load_sources=lambda ws: []).run_once()
    assert summary is not None and summary.reports == [] and summary.acked
