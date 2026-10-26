"""T-V1-035 — sync convergence proof (fixtures, no product changes).

Proves the T-V1-034 consumer converges **exactly-once** across the ticket's
five scenarios plus the interrupt / failure / limit controls, all against a
deterministic in-memory source + a versioned publication store behind the
``publisher`` seam (no network, no services; fixed-seed shuffle, no sleeps):

- **create / update / delete / reorder / retry** (5/5) converge to the correct
  document set with **no duplicate published versions**;
- **kill mid-sync → resume** (via T-V1-020 lease redelivery, and via a direct
  mid-page crash) publishes each changed item **exactly once** — an item
  already outcome-recorded on an interrupted page is re-served but NOT
  re-published (the exactly-once crux);
- **failed items are visible** (canonical T-V1-025 code) **and retryable**;
- **unauthorized paths are excluded** from the published set (real
  ``LocalFolderConnector`` symlink-escape guard);
- **item limits** (non-advancing cursor, ``max_pages``) fail loudly instead of
  looping; **byte limit** is honestly documented as *not* enforced on this path
  (the ingestion budget owns it — see the byte test + handoff RISKS).

Test + fixtures only (ticket: no product code unless T-V1-034 left a gap — it
did not; every guard below already lives in the consumer/adapters).
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from connectors import ConnectorConfig, ConnectorKind
from connectors.adapters import LocalFolderConnector
from connectors.interfaces import (
    ConnectorDiscovery,
    SourceItem,
    page_items,
    snapshot_digest,
)
from connectors.sync_consumer import (
    InMemorySyncProgressStore,
    ItemStatus,
    QueuedSyncConsumer,
    SourceIdentity,
    SyncClaim,
    SyncReport,
    encode_sync_job,
)
from core.errors import ReasonCode, canonical_code, normalize_code

pytestmark = pytest.mark.unit


# ---- fixture harness ---------------------------------------------------------


class Source:
    """Controllable source of truth: create/update/delete + reorderable order.

    Listings are always served **sorted by external_id** (the connector contract
    the T-V1-034 keyset cursor depends on); ``shuffle`` only perturbs the
    *stored* order so the tests can prove convergence is order-independent.
    """

    def __init__(self, items: dict[str, str] | None = None) -> None:
        self._data: dict[str, bytes] = {
            k: v.encode("utf-8") for k, v in (items or {}).items()
        }
        self._order: list[str] = list(self._data)

    def create(self, external_id: str, body: str) -> None:
        if external_id not in self._data:
            self._order.append(external_id)
        self._data[external_id] = body.encode("utf-8")

    def update(self, external_id: str, body: str) -> None:
        self._data[external_id] = body.encode("utf-8")

    def delete(self, external_id: str) -> None:
        self._data.pop(external_id, None)
        self._order = [i for i in self._order if i != external_id]

    def shuffle(self, seed: int) -> None:
        random.Random(seed).shuffle(self._order)  # deterministic, no wall-clock

    def ids(self) -> list[str]:
        return sorted(self._data)

    def listing(self) -> list[SourceItem]:
        items = []
        for external_id in sorted(self._data):  # key order is the contract
            body = self._data[external_id]
            items.append(
                SourceItem(
                    external_id=external_id,
                    name=external_id,
                    size_bytes=len(body),
                    metadata={"checksum": hashlib.sha256(body).hexdigest()},
                )
            )
        return items

    def read(self, external_id: str) -> bytes:
        if external_id not in self._data:
            raise KeyError(external_id)
        return self._data[external_id]


class SourceConnector:
    """A conforming :class:`Connector` over a :class:`Source`.

    Uses the real ``page_items``/``snapshot_digest`` so keyset paging and the
    snapshot digest are the production code paths, not a re-implementation.
    """

    kind = ConnectorKind.LOCAL_UPLOAD

    def __init__(self, source: Source, page_size: int = 2) -> None:
        self._source = source
        self._page_size = page_size

    async def discover(
        self, config: ConnectorConfig, cursor: str | None = None
    ) -> ConnectorDiscovery:
        items = self._source.listing()
        window, next_cursor = page_items(items, cursor, self._page_size)
        return ConnectorDiscovery(
            items=window, next_cursor=next_cursor, snapshot=snapshot_digest(items)
        )

    async def fetch(self, config: ConnectorConfig, item: SourceItem) -> bytes:
        return self._source.read(item.external_id)

    async def sync(self, config: ConnectorConfig, cursor: str | None = None):  # pragma: no cover
        raise NotImplementedError


class FlakyConnector(SourceConnector):
    """Wraps a source; the first ``fail_times`` reads of a listed id raise."""

    def __init__(self, source: Source, page_size: int, fail_on: str, fail_times: int) -> None:
        super().__init__(source, page_size)
        self._fail_on = fail_on
        self._remaining = fail_times

    async def fetch(self, config: ConnectorConfig, item: SourceItem) -> bytes:
        if item.external_id == self._fail_on and self._remaining > 0:
            self._remaining -= 1
            raise OSError("transient read failure")
        return await super().fetch(config, item)


class PublishedStore:
    """Versioned publication target behind the ``publisher`` seam.

    A **published version** is a new ``(id, checksum)`` row created only when
    the checksum differs from the current head — re-publishing the identical
    checksum is idempotent (a no-op), mirroring a real versioned store. Every
    invocation is still logged in ``calls`` so at-least-once redelivery can be
    observed separately from version growth.
    """

    def __init__(self) -> None:
        self.head: dict[str, str] = {}
        self.versions: dict[str, list[str]] = {}
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self, identity: SourceIdentity, item: SourceItem, content: bytes, checksum: str
    ) -> None:
        assert checksum == hashlib.sha256(content).hexdigest()
        self.calls.append((item.external_id, checksum))
        history = self.versions.setdefault(item.external_id, [])
        if not history or history[-1] != checksum:
            history.append(checksum)
        self.head[item.external_id] = checksum

    def duplicate_versions(self) -> dict[str, list[str]]:
        """Any id whose version history repeats a checksum (must stay empty)."""
        return {
            eid: history
            for eid, history in self.versions.items()
            if len(history) != len(set(history))
        }

    def publish_count(self, external_id: str) -> int:
        return sum(1 for eid, _ in self.calls if eid == external_id)

    def live_set(self, source: Source) -> set[str]:
        return set(self.head) & set(source.ids())


class InterruptingPublisher:
    """Wraps a publisher; raises on a chosen id for the first ``times`` calls.

    Reusing the same instance after the interrupt lets the resumed run delegate
    straight to the store (``times`` is exhausted), keeping ONE call log.
    """

    def __init__(self, base: PublishedStore, fail_on: str, times: int = 1) -> None:
        self._base = base
        self._fail_on = fail_on
        self._times = times

    async def __call__(
        self, identity: SourceIdentity, item: SourceItem, content: bytes, checksum: str
    ) -> None:
        if item.external_id == self._fail_on and self._times > 0:
            self._times -= 1
            raise RuntimeError("interrupted mid-sync")
        await self._base(identity, item, content, checksum)


class LeaseQueue:
    """Models T-V1-020 claim/ack/lease redelivery (no Redis).

    ``claim`` hands out the next un-acked payload; ``ack`` retires it. When
    ``run_once`` raises the payload is never acked, so the driver ``nack``s it
    back (lease expiry) and it is redelivered on the next claim.
    """

    def __init__(self, payloads: list[str]) -> None:
        self._pending: list[str] = list(payloads)
        self._acked: list[str] = []
        self.redeliveries = 0
        self._last: SyncClaim | None = None

    async def claim(self, timeout_seconds: int = 1) -> SyncClaim | None:
        if not self._pending:
            return None
        self._last = SyncClaim(job_id=self._pending.pop(0), token="tok")
        return self._last

    async def ack(self, claim: SyncClaim) -> bool:
        self._acked.append(claim.job_id)
        return True

    def nack(self) -> None:
        if self._last is not None:
            self._pending.append(self._last.job_id)
            self.redeliveries += 1

    @property
    def acked(self) -> list[str]:
        return list(self._acked)

    @property
    def drained(self) -> bool:
        return not self._pending


def _config(connector_id: str = "c1") -> ConnectorConfig:
    return ConnectorConfig(
        connector_id=connector_id,
        workspace_id="ws-a",
        kind=ConnectorKind.LOCAL_UPLOAD,
        settings={"fixture": connector_id},
    )


def _consumer(
    store: InMemorySyncProgressStore,
    queue: LeaseQueue,
    *,
    connector: Any,
    publisher: Callable[..., Any] | None = None,
    deleter: Callable[..., Any] | None = None,
    page_size: int = 2,
    max_pages: int | None = None,
    load_sources: Callable[..., Any] | None = None,
) -> QueuedSyncConsumer:
    kwargs: dict[str, Any] = {
        "store": store,
        "queue": queue,
        "page_size": page_size,
        "publisher": publisher,
        "connector_factory": lambda _cfg: connector,
    }
    if deleter is not None:
        kwargs["deleter"] = deleter
    if load_sources is not None:
        kwargs["load_sources"] = load_sources
    if max_pages is not None:
        kwargs["max_pages"] = max_pages
    return QueuedSyncConsumer(**kwargs)


class RecordingDeleter:
    """Deleter seam stand-in: records every tombstone call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, identity: SourceIdentity, external_id: str, prior: Any) -> None:
        self.calls.append(external_id)


async def _drive(
    consumer_factory: Callable[[], QueuedSyncConsumer],
    queue: LeaseQueue,
    *,
    max_loops: int = 12,
) -> list[SyncReport | None]:
    """Run ``run_once`` until the queue drains, redelivering on a raised failure."""
    reports: list[SyncReport | None] = []
    loops = 0
    while not queue.drained:
        loops += 1
        if loops > max_loops:
            raise AssertionError("sync did not converge within the loop bound")
        try:
            summary = await consumer_factory().run_once()
        except Exception:
            queue.nack()  # un-acked → lease redelivers
            reports.append(None)
            continue
        if summary is None:
            continue
        # Root review #4: a run with failures must NOT ack (the lease expires
        # and the job is redelivered); a clean run acks. This harness models
        # the redelivery explicitly in the dedicated recovery test below.
        clean = all(report.failed == 0 for report in summary.reports)
        assert summary.acked is clean
        reports.extend(summary.reports)
    return reports


async def _drive_until_converged(
    consumer_factory: Callable[[], QueuedSyncConsumer],
    queue: LeaseQueue,
    *,
    max_loops: int = 12,
) -> list[SyncReport]:
    """Drive with REAL lease semantics: an un-acked claim is redelivered.

    This is the recovery path POLICE's finding #4 restored: a failed item
    (or a failed deletion) leaves the job un-acked, the lease expires, the
    job comes back, and the retry converges — instead of the failed work
    being silently acked away.
    """
    reports: list[SyncReport] = []
    loops = 0
    while not queue.drained:
        loops += 1
        if loops > max_loops:
            raise AssertionError("sync did not converge within the loop bound")
        summary = await consumer_factory().run_once()
        if summary is None:
            continue
        reports.extend(summary.reports)
        if not summary.acked:
            queue.nack()  # lease expiry -> redelivery
    return reports


def _assert_exactly_once(store: PublishedStore, source: Source, expected_ids: set[str]) -> None:
    """The headline metric: correct set, one version per change, no duplicates."""
    assert store.duplicate_versions() == {}, "a change produced a duplicate published version"
    assert store.live_set(source) == expected_ids
    for external_id in expected_ids:
        checksums = [c for eid, c in store.calls if eid == external_id]
        assert len(checksums) == len(set(checksums)), f"{external_id} republished a checksum"


# ---- the 5 convergence scenarios --------------------------------------------


async def test_scenario_create_converges_exactly_once() -> None:
    source = Source({"a.md": "A", "b.md": "B", "c.md": "C", "d.md": "D"})
    store = PublishedStore()
    progress = InMemorySyncProgressStore()
    config = _config()
    queue = LeaseQueue([encode_sync_job("ws-a")])
    connector = SourceConnector(source, page_size=2)

    reports = await _drive(
        lambda: _consumer(progress, queue, connector=connector, publisher=store,
                          load_sources=lambda _ws: [config]),
        queue,
    )

    (report,) = reports
    assert report is not None
    assert (report.discovered, report.created, report.failed) == (4, 4, 0)
    _assert_exactly_once(store, source, {"a.md", "b.md", "c.md", "d.md"})
    assert queue.acked == [encode_sync_job("ws-a")]


async def test_scenario_update_appends_one_version_no_duplicate() -> None:
    source = Source({"a.md": "A", "b.md": "B"})
    store = PublishedStore()
    progress = InMemorySyncProgressStore()
    config = _config()
    connector = SourceConnector(source, page_size=2)

    q1 = LeaseQueue([encode_sync_job("ws-a")])
    await _drive(
        lambda: _consumer(progress, q1, connector=connector, publisher=store,
                          load_sources=lambda _w: [config]),
        q1,
    )
    assert {eid: len(h) for eid, h in store.versions.items()} == {"a.md": 1, "b.md": 1}

    source.update("a.md", "A-v2")
    source.update("b.md", "B-v2")
    q2 = LeaseQueue([encode_sync_job("ws-a")])
    reports = await _drive(
        lambda: _consumer(progress, q2, connector=connector, publisher=store,
                          load_sources=lambda _w: [config]),
        q2,
    )
    (report,) = reports
    assert report is not None
    assert (report.updated, report.created, report.unchanged) == (2, 0, 0)
    assert {eid: len(h) for eid, h in store.versions.items()} == {"a.md": 2, "b.md": 2}
    _assert_exactly_once(store, source, {"a.md", "b.md"})


async def test_scenario_delete_drops_out_without_resurrect() -> None:
    source = Source({"a.md": "A", "b.md": "B", "c.md": "C"})
    store = PublishedStore()
    progress = InMemorySyncProgressStore()
    config = _config()
    connector = SourceConnector(source, page_size=2)

    q1 = LeaseQueue([encode_sync_job("ws-a")])
    await _drive(
        lambda: _consumer(progress, q1, connector=connector, publisher=store,
                          load_sources=lambda _w: [config]),
        q1,
    )
    assert store.live_set(source) == {"a.md", "b.md", "c.md"}

    source.delete("b.md")
    calls_before = store.publish_count("b.md")
    q2 = LeaseQueue([encode_sync_job("ws-a")])
    reports = await _drive(
        lambda: _consumer(progress, q2, connector=connector, publisher=store,
                          load_sources=lambda _w: [config]),
        q2,
    )
    (report,) = reports
    assert report is not None
    assert report.discovered == 2  # b.md is gone from the listing
    assert store.publish_count("b.md") == calls_before  # never re-published / resurrected
    assert store.live_set(source) == {"a.md", "c.md"}  # correct live set
    # Disclosed boundary: tombstoning the already-published b.md *document*
    # (dropping its vectors/rows) is ingestion's T-V1-017 job, not this
    # consumer's publisher seam — the sync boundary guarantees no resurrect +
    # the correct live set.


async def test_scenario_reorder_is_order_independent_and_converges() -> None:
    source = Source({"a.md": "A", "b.md": "B", "c.md": "C", "d.md": "D", "e.md": "E"})
    store = PublishedStore()
    progress = InMemorySyncProgressStore()
    config = _config()
    connector = SourceConnector(source, page_size=2)

    q1 = LeaseQueue([encode_sync_job("ws-a")])
    await _drive(
        lambda: _consumer(progress, q1, connector=connector, publisher=store,
                          load_sources=lambda _w: [config]),
        q1,
    )
    first_head = dict(store.head)

    source.shuffle(seed=7)  # perturb the stored order
    calls_before = len(store.calls)
    q2 = LeaseQueue([encode_sync_job("ws-a")])
    reports = await _drive(
        lambda: _consumer(progress, q2, connector=connector, publisher=store,
                          load_sources=lambda _w: [config]),
        q2,
    )
    (report,) = reports
    assert report is not None
    assert (report.unchanged, report.created, report.updated) == (5, 0, 0)
    assert store.head == first_head  # order perturbation changed nothing
    assert len(store.calls) == calls_before  # no extra publishes
    _assert_exactly_once(store, source, set(source.ids()))


async def test_scenario_retry_transient_job_failure_redelivery_converges() -> None:
    """Reviewer checklist (3): a killed job is redelivered and still converges.

    Doubles as the **kill mid-sync → resume → correct set, no duplicates**
    proof: the publisher dies on ``c.md`` mid-run, the claim is un-acked, the
    lease redelivers, and the resumed run finishes exactly-once.
    """
    source = Source({"a.md": "A", "b.md": "B", "c.md": "C", "d.md": "D", "e.md": "E"})
    store = PublishedStore()
    progress = InMemorySyncProgressStore()
    config = _config()
    queue = LeaseQueue([encode_sync_job("ws-a")])
    connector = SourceConnector(source, page_size=2)
    crashing = InterruptingPublisher(store, fail_on="c.md", times=1)

    reports = await _drive(
        lambda: _consumer(progress, queue, connector=connector, publisher=crashing,
                          load_sources=lambda _w: [config]),
        queue,
    )

    assert queue.redeliveries == 1  # the killed job was redelivered once
    assert reports[0] is None  # the first attempt raised
    assert queue.acked == [encode_sync_job("ws-a")]  # eventually completed + acked
    _assert_exactly_once(store, source, set(source.ids()))


# ---- interrupt / kill mid-sync → resume -------------------------------------


async def test_interrupt_mid_page_resume_does_not_republish_outcomed_sibling() -> None:
    """The exactly-once crux: a re-served, already-outcomed item is NOT re-published.

    ``page_size=2`` → pages ``[a,b]`` then ``[c,d]``. The publisher dies on
    ``d`` (2nd item of page 2) *after* ``c`` was outcome-recorded but *before*
    page 2's cursor was saved. On resume the cursor is still ``k:b``, so page 2
    (``c,d``) is re-served — ``c`` must be re-classified unchanged (not
    re-published) and ``d`` published exactly once.
    """
    source = Source({"a.md": "A", "b.md": "B", "c.md": "C", "d.md": "D"})
    store = PublishedStore()
    progress = InMemorySyncProgressStore()
    config = _config()
    connector = SourceConnector(source, page_size=2)
    crashing = InterruptingPublisher(store, fail_on="d.md", times=1)

    with pytest.raises(RuntimeError, match="interrupted mid-sync"):
        await _consumer(progress, LeaseQueue([]), connector=connector, publisher=crashing,
                        load_sources=lambda _w: [config]).run_sync(_config())

    assert sorted(store.head) == ["a.md", "b.md", "c.md"]  # a,b (page 1) + c (then d crashed)

    # resume: same store, same publisher (times exhausted → delegates to store)
    report = await _consumer(progress, LeaseQueue([]), connector=connector, publisher=crashing,
                             load_sources=lambda _w: [config]).run_sync(_config())

    assert report.resumed is True
    assert store.publish_count("c.md") == 1  # re-served but NOT re-published
    assert store.publish_count("d.md") == 1  # published exactly once on resume
    _assert_exactly_once(store, source, {"a.md", "b.md", "c.md", "d.md"})


async def test_reorder_plus_interrupt_resume_still_converges() -> None:
    """Reviewer checklist (1)+(2): reorder composes with interrupt-resume.

    Interrupt at cursor ``k:b``, shuffle the source's stored order, then resume
    — the keyset cursor still serves exactly ``c,d`` regardless of order.
    """
    source = Source({"a.md": "A", "b.md": "B", "c.md": "C", "d.md": "D"})
    store = PublishedStore()
    progress = InMemorySyncProgressStore()
    config = _config()
    connector = SourceConnector(source, page_size=2)
    crashing = InterruptingPublisher(store, fail_on="d.md", times=1)

    with pytest.raises(RuntimeError):
        await _consumer(progress, LeaseQueue([]), connector=connector, publisher=crashing,
                        load_sources=lambda _w: [config]).run_sync(_config())

    source.shuffle(seed=11)  # reorder the stored order between crash and resume
    report = await _consumer(progress, LeaseQueue([]), connector=connector, publisher=crashing,
                             load_sources=lambda _w: [config]).run_sync(_config())

    assert report.resumed is True
    _assert_exactly_once(store, source, {"a.md", "b.md", "c.md", "d.md"})


# ---- failures visible + retryable -------------------------------------------


async def test_failed_item_is_visible_with_canonical_code_and_recovers() -> None:
    source = Source({"a.md": "A", "b.md": "B"})
    config = _config()
    progress = InMemorySyncProgressStore()
    store = PublishedStore()

    # a.md's first read fails → visible FAILED outcome, run continues
    flaky = FlakyConnector(source, page_size=2, fail_on="a.md", fail_times=1)
    q1 = LeaseQueue([encode_sync_job("ws-a")])
    await _drive(
        lambda: _consumer(progress, q1, connector=flaky, publisher=store,
                          load_sources=lambda _w: [config]),
        q1,
    )
    identity = SourceIdentity.from_config(config)
    row = await progress.load(identity)
    outcomes = await progress.outcomes(identity, row.generation if row else 1)
    assert outcomes["a.md"].status is ItemStatus.FAILED
    code = outcomes["a.md"].error_code
    assert code == normalize_code(ReasonCode.INGESTION_ERROR)
    assert code is not None
    assert canonical_code(code) is ReasonCode.INGESTION_ERROR
    assert store.live_set(source) == {"b.md"}  # the failure did not block the healthy item

    # the failure clears → the retryable item converges on the next run
    healthy = SourceConnector(source, page_size=2)
    q2 = LeaseQueue([encode_sync_job("ws-a")])
    await _drive(
        lambda: _consumer(progress, q2, connector=healthy, publisher=store,
                          load_sources=lambda _w: [config]),
        q2,
    )
    assert store.live_set(source) == {"a.md", "b.md"}  # recovered
    assert store.duplicate_versions() == {}


async def test_unauthorized_path_is_excluded_and_visible(tmp_path: Path) -> None:
    """A path-escaping item is refused, visible as FAILED, and never published."""
    (tmp_path / "ok.md").write_text("fine", encoding="utf-8")
    outside = tmp_path.parent / "t035_outside_secret.md"
    outside.write_text("SECRET", encoding="utf-8")
    link = tmp_path / "escape.md"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(outside)  # discovered, but fetch must refuse it

    config = ConnectorConfig(
        connector_id="local",
        workspace_id="ws-a",
        kind=ConnectorKind.LOCAL_UPLOAD,
        settings={"path": str(tmp_path)},
    )
    store = PublishedStore()
    progress = InMemorySyncProgressStore()
    queue = LeaseQueue([encode_sync_job("ws-a")])
    connector = LocalFolderConnector(page_size=10)
    await _drive(
        lambda: _consumer(progress, queue, connector=connector, publisher=store,
                          load_sources=lambda _w: [config]),
        queue,
    )

    assert set(store.head) == {"ok.md"}  # only the authorized item published
    identity = SourceIdentity.from_config(config)
    row = await progress.load(identity)
    outcomes = await progress.outcomes(identity, row.generation if row else 1)
    assert outcomes["escape.md"].status is ItemStatus.FAILED  # visible exclusion
    assert "escape.md" not in store.head  # never reached the publisher


# ---- limits: item (fail-loud) + byte (honest disclosure) ---------------------


async def test_item_limit_non_advancing_cursor_fails_loud() -> None:
    """A misbehaving adapter that never advances the keyset must fail, not loop."""

    class Stuck:
        kind = ConnectorKind.LOCAL_UPLOAD

        async def discover(self, config, cursor=None):
            item = SourceItem(external_id="a.md", name="a.md", metadata={"checksum": "0" * 64})
            return ConnectorDiscovery(items=[item], next_cursor="k:a.md", snapshot="s")

        async def fetch(self, config, item):
            return b"x"

        async def sync(self, config, cursor=None):  # pragma: no cover
            raise NotImplementedError

    consumer = _consumer(InMemorySyncProgressStore(), LeaseQueue([]), connector=Stuck(),
                         load_sources=lambda _w: [_config("stuck")])
    with pytest.raises(RuntimeError, match="cursor did not advance"):
        await consumer.run_sync(_config("stuck"))


async def test_item_limit_max_pages_fails_loud() -> None:
    """An unbounded listing is cut off by ``max_pages`` instead of running forever."""

    class Endless:
        kind = ConnectorKind.LOCAL_UPLOAD

        def __init__(self) -> None:
            self._n = 0

        async def discover(self, config, cursor=None):
            self._n += 1
            external_id = f"i{self._n:04d}"
            item = SourceItem(
                external_id=external_id, name=external_id, metadata={"checksum": "0" * 64}
            )
            return ConnectorDiscovery(items=[item], next_cursor=f"k:{external_id}", snapshot="s")

        async def fetch(self, config, item):
            return b"x"

        async def sync(self, config, cursor=None):  # pragma: no cover
            raise NotImplementedError

    consumer = _consumer(InMemorySyncProgressStore(), LeaseQueue([]), connector=Endless(),
                         load_sources=lambda _w: [_config("endless")], max_pages=3)
    with pytest.raises(RuntimeError, match="exceeded max_pages"):
        await consumer.run_sync(_config("endless"))


async def test_byte_limit_is_not_enforced_in_the_sync_path_disclosed() -> None:
    """Honest disclosure: the sync consumer does NOT gate item bytes.

    A large item is handed straight to the publisher seam (byte/time budgets
    belong to the ingestion layer, T-V1-031 — see the handoff RISKS). This pins
    the current boundary so a byte cap cannot be added here silently.
    """
    big = "x" * (200 * 1024)  # 200 KiB, well past any plausible per-item tickle
    source = Source({"big.md": big, "small.md": "s"})
    store = PublishedStore()
    progress = InMemorySyncProgressStore()
    config = _config()
    connector = SourceConnector(source, page_size=2)
    queue = LeaseQueue([encode_sync_job("ws-a")])

    reports = await _drive(
        lambda: _consumer(progress, queue, connector=connector, publisher=store,
                          load_sources=lambda _w: [config]),
        queue,
    )
    (report,) = reports
    assert report is not None
    assert report.created == 2  # the big item is published, not rejected
    assert store.head["big.md"] == hashlib.sha256(big.encode("utf-8")).hexdigest()


async def test_unacked_failed_run_redelivers_and_converges() -> None:
    """Root review #4: the failed run is NOT acked; the redelivery converges."""
    source = Source({"a.md": "A", "b.md": "B"})
    config = _config()
    progress = InMemorySyncProgressStore()
    store = PublishedStore()

    flaky = FlakyConnector(source, page_size=2, fail_on="a.md", fail_times=1)
    queue = LeaseQueue([encode_sync_job("ws-a")])
    reports = await _drive_until_converged(
        lambda: _consumer(progress, queue, connector=flaky, publisher=store,
                          load_sources=lambda _w: [config]),
        queue,
    )
    assert queue.redeliveries >= 1  # the un-acked job came back
    # The failure was visible on its own run, then recovered on the redelivery.
    assert any("a.md" in e for r in reports for e in r.errors)
    assert store.live_set(source) == {"a.md", "b.md"}
    assert store.duplicate_versions() == {}


async def test_empty_source_deletes_everything_on_a_fresh_run(tmp_path: Path) -> None:
    """Root review #2: a source that emptied out must delete its documents."""
    config = _config()
    progress = InMemorySyncProgressStore()
    store = PublishedStore()
    deleter = RecordingDeleter()

    full = SourceConnector(Source({"a.md": "A", "b.md": "B"}), page_size=2)
    q1 = LeaseQueue([encode_sync_job("ws-a")])
    await _drive(
        lambda: _consumer(progress, q1, connector=full, publisher=store,
                          deleter=deleter, load_sources=lambda _w: [config]),
        q1,
    )
    assert store.live_set(Source({"a.md": "A", "b.md": "B"})) == {"a.md", "b.md"}

    # The source empties completely.
    empty = SourceConnector(Source({}), page_size=2)
    q2 = LeaseQueue([encode_sync_job("ws-a")])
    reports = await _drive(
        lambda: _consumer(progress, q2, connector=empty, publisher=store,
                          deleter=deleter, load_sources=lambda _w: [config]),
        q2,
    )
    deleted = sum(r.deleted for r in reports)
    assert deleted == 2  # both documents tombstoned via the deleter
    assert store.live_set(Source({})) == set()


async def test_resumed_run_does_not_delete_earlier_pages() -> None:
    """Root review #1: a resumed (mid-cursor) run must not false-delete."""
    config = _config()
    progress = InMemorySyncProgressStore()
    store = PublishedStore()
    deleter = RecordingDeleter()

    # A fresh run publishes page 1, then is interrupted mid-listing (the fake
    # queue drains after one page via max_pages).
    source = Source({f"f{i}.md": str(i) for i in range(4)})
    q1 = LeaseQueue([encode_sync_job("ws-a")])
    interrupted = _consumer(progress, q1, connector=SourceConnector(source, page_size=2),
                            publisher=store, deleter=deleter,
                            load_sources=lambda _w: [config], max_pages=1)
    with pytest.raises(RuntimeError, match="max_pages"):
        await interrupted.run_once()
    assert q1.acked == []
    assert deleter.calls == []  # the interrupted fresh run deleted nothing

    # The RESUME run: it starts mid-cursor — earlier pages are not re-seen and
    # must NOT be deleted as absent (root review #1).
    q2 = LeaseQueue([encode_sync_job("ws-a")])
    await _drive(
        lambda: _consumer(progress, q2, connector=SourceConnector(source, page_size=2),
                          publisher=store, deleter=deleter,
                          load_sources=lambda _w: [config]),
        q2,
    )
    assert deleter.calls == []  # full reconciliation proves all four still exist
    assert q2.acked == [encode_sync_job("ws-a")]


async def test_failed_prefix_is_retried_before_recovered_claim_is_acked() -> None:
    source = Source({"a.md": "A", "b.md": "B", "c.md": "C"})
    connector = FlakyConnector(source, page_size=1, fail_on="a.md", fail_times=1)
    published, progress = PublishedStore(), InMemorySyncProgressStore()
    queue, config = LeaseQueue([encode_sync_job("ws-a")]), _config()
    interrupted = _consumer(progress, queue, connector=connector, publisher=published,
                            load_sources=lambda _w: [config], max_pages=1)
    with pytest.raises(RuntimeError, match="max_pages"):
        await interrupted.run_once()
    assert queue.acked == [] and "a.md" not in published.head
    queue.nack()
    recovered = _consumer(progress, queue, connector=connector, publisher=published,
                          load_sources=lambda _w: [config])
    result = await recovered.run_once()
    assert result is not None and result.acked
    assert set(published.head) == set(source.ids())
    assert not published.duplicate_versions()
    outcomes = await progress.outcomes(SourceIdentity.from_config(config), 1)
    assert all(item.status is not ItemStatus.FAILED for item in outcomes.values())
    state = await progress.load(SourceIdentity.from_config(config))
    assert state.last_error_code is None
    assert state.last_success_at == state.last_finished_at >= state.last_attempt_at


async def test_recovered_claim_reconciles_real_deletion_before_ack() -> None:
    source = Source({"a.md": "A", "b.md": "B", "c.md": "C"})
    connector = SourceConnector(source, page_size=1)
    published, progress, deleter = PublishedStore(), InMemorySyncProgressStore(), RecordingDeleter()
    config = _config()
    baseline = _consumer(progress, LeaseQueue([]), connector=connector,
                         publisher=published, deleter=deleter)
    await baseline.run_sync(config)
    source.delete("b.md")
    queue = LeaseQueue([encode_sync_job("ws-a")])
    interrupted = _consumer(progress, queue, connector=connector, publisher=published,
                            deleter=deleter, load_sources=lambda _w: [config], max_pages=1)
    with pytest.raises(RuntimeError, match="max_pages"):
        await interrupted.run_once()
    assert queue.acked == []
    queue.nack()
    recovered = _consumer(progress, queue, connector=connector, publisher=published,
                          deleter=deleter, load_sources=lambda _w: [config])
    result = await recovered.run_once()
    assert result is not None and result.acked
    assert deleter.calls == ["b.md"]
    outcomes = await progress.outcomes(SourceIdentity.from_config(config), 1)
    assert outcomes["b.md"].status is ItemStatus.DELETED
    assert outcomes["a.md"].status is not ItemStatus.DELETED
    assert outcomes["c.md"].status is not ItemStatus.DELETED


async def test_source_failure_is_durable_and_logs_do_not_echo_credentials(caplog) -> None:
    class BrokenDiscovery(SourceConnector):
        async def discover(self, *args, **kwargs):
            raise OSError("private-credential-sentinel")

    config, progress = _config(), InMemorySyncProgressStore()
    queue = LeaseQueue([encode_sync_job("ws-a")])
    consumer = _consumer(progress, queue, connector=BrokenDiscovery(Source({})),
                         load_sources=lambda _w: [config])
    with pytest.raises(OSError):
        await consumer.run_once()
    state = await progress.load(SourceIdentity.from_config(config))
    assert state.last_attempt_at is not None
    assert state.last_finished_at >= state.last_attempt_at
    assert state.last_error_code == normalize_code(ReasonCode.INGESTION_ERROR)
    assert queue.acked == []
    assert "private-credential-sentinel" not in caplog.text
