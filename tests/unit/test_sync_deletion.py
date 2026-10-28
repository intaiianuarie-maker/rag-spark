"""Behavioral tests for M6-001b deletion propagation (deleter seam)."""

from __future__ import annotations

import hashlib

import pytest
from connectors import ConnectorConfig, ConnectorKind
from connectors.interfaces import ConnectorDiscovery, SourceItem, page_items, snapshot_digest
from connectors.sync_consumer import (
    InMemorySyncProgressStore,
    ItemStatus,
    QueuedSyncConsumer,
    SourceIdentity,
    SyncClaim,
    encode_sync_job,
)

pytestmark = pytest.mark.unit


class FakeQueue:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.acked = []

    async def claim(self, timeout_seconds: int = 1):
        if not self._payloads:
            return None
        c = SyncClaim(job_id=self._payloads.pop(0), token="tok")
        return c

    async def ack(self, claim):
        self.acked.append(claim)
        return True


class Source:
    def __init__(self, items):
        self._data = dict(items)

    def delete(self, eid):
        self._data.pop(eid, None)

    def listing(self):
        out = []
        for eid in sorted(self._data):
            body = self._data[eid].encode()
            out.append(
                SourceItem(
                    external_id=eid,
                    name=eid,
                    size_bytes=len(body),
                    metadata={"checksum": hashlib.sha256(body).hexdigest()},
                )
            )
        return out

    def read(self, eid):
        return self._data[eid].encode()


class Connector:
    kind = ConnectorKind.LOCAL_UPLOAD
    page_size = 10

    def __init__(self, source):
        self._source = source

    async def discover(self, config, cursor=None):
        items = self._source.listing()
        window, nxt = page_items(items, cursor, self.page_size)
        return ConnectorDiscovery(items=window, next_cursor=nxt, snapshot=snapshot_digest(items))

    async def fetch(self, config, item):
        return self._source.read(item.external_id)


def _config():
    return ConnectorConfig(
        connector_id="c1", workspace_id="ws-a", kind=ConnectorKind.LOCAL_UPLOAD, settings={}
    )


@pytest.mark.asyncio
async def test_deletion_propagation_end_to_end():
    source = Source({"a.md": "A", "b.md": "B", "c.md": "C"})
    store = InMemorySyncProgressStore()
    deleted_calls = []

    async def deleter(identity, external_id, prior):
        deleted_calls.append(external_id)

    connector = Connector(source)
    cfg = _config()

    # run 1
    q1 = FakeQueue([encode_sync_job("ws-a")])
    consumer = QueuedSyncConsumer(
        store=store,
        queue=q1,
        connector_factory=lambda _c: connector,
        load_sources=lambda _w: [cfg],
        deleter=deleter,
    )
    reports = []
    while not q1._payloads or True:
        r = await consumer.run_once(timeout_seconds=1)
        if r is None:
            break
        reports.append(r)
        if not q1._payloads:
            break
    assert reports and reports[0].reports[0].discovered == 3

    # delete b
    source.delete("b.md")
    q2 = FakeQueue([encode_sync_job("ws-a")])
    consumer2 = QueuedSyncConsumer(
        store=store,
        queue=q2,
        connector_factory=lambda _c: connector,
        load_sources=lambda _w: [cfg],
        deleter=deleter,
    )
    reports2 = []
    while True:
        r = await consumer2.run_once(timeout_seconds=1)
        if r is None:
            break
        reports2.append(r)
        if not q2._payloads:
            break
    rep = reports2[0].reports[0]
    assert rep.discovered == 2
    assert rep.deleted == 1
    assert deleted_calls == ["b.md"]

    # outcome recorded as DELETED
    identity = None
    identity = SourceIdentity.from_config(cfg)
    outcomes = await store.outcomes(identity, 1)
    assert outcomes["b.md"].status is ItemStatus.DELETED

    # progress items_deleted
    progress = await store.load(identity)
    assert progress.items_deleted == 1


@pytest.mark.asyncio
async def test_worker_entrypoint_run_once(monkeypatch):
    from apps.worker.jobs import sync_worker

    class FakeQueue:
        def __init__(self, payloads):
            self._payloads = list(payloads)
            self.acked = []

        async def claim(self, timeout_seconds=1):
            if not self._payloads:
                return None
            c = SyncClaim(job_id=self._payloads.pop(0), token="tok")
            return c

        async def ack(self, claim):
            self.acked.append(claim)
            return True

    fq = FakeQueue([encode_sync_job("ws-a")])

    class FakeRedisQueue:
        def __init__(self):
            pass

        async def claim(self, timeout_seconds=1):
            return await fq.claim(timeout_seconds)

        async def ack(self, claim):
            return await fq.ack(claim)

    monkeypatch.setattr(sync_worker, "RedisSyncQueue", lambda: FakeRedisQueue())

    # provide a source via env
    source = Source({"x.md": "X"})
    connector = Connector(source)
    cfg = _config()

    store = InMemorySyncProgressStore()

    # monkeypatch QueuedSyncConsumer to inject store/connector/load_sources
    original_consumer = sync_worker.QueuedSyncConsumer

    class PatchedConsumer(original_consumer):
        def __init__(self, *a, **kw):
            kw["store"] = store
            kw["connector_factory"] = lambda _c: connector
            kw["load_sources"] = lambda _w: [cfg]
            super().__init__(*a, **kw)

    monkeypatch.setattr(sync_worker, "QueuedSyncConsumer", PatchedConsumer)

    # Helper-only test: inject a no-op publisher/deleter so the entrypoint's
    # PRODUCTION defaults (which resolve a real document session and a valid
    # workspace UUID) are not exercised against the "ws-a" stub workspace.
    async def _noop(*args, **kwargs):
        return None

    result = await sync_worker.run_sync_once(
        timeout_seconds=1, store=store, publisher=_noop, deleter=_noop
    )
    assert result["claimed"] is True
    assert result["acked"] is True
    assert result["reports"][0]["discovered"] == 1
