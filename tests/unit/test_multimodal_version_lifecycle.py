"""Multimodal page vectors follow the generation lifecycle (plain sync; asyncio.run).

The multimodal step runs only after READY committed, re-checks liveness after
its upsert, and every version-scoped cleanup (fence unpublish, supersede) also
removes that version's ``knowledge_multimodal`` points — scoped by workspace AND
document AND version. The store half runs on a real in-memory Qdrant
(qdrant-client ``:memory:``) so the payload filters are exercised, not faked.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import uuid
from typing import ClassVar

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion import dedup
from jobs import ingest_pipeline as ip
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from retrieval.adapters.qdrant_store import QdrantVectorStore, VectorStoreError

pytestmark = pytest.mark.unit

MM = "knowledge_multimodal"
WS, WS2 = uuid.uuid4(), uuid.uuid4()
DOC, DOC2 = uuid.uuid4(), uuid.uuid4()
V1, V2 = uuid.uuid4(), uuid.uuid4()


class _MemStore(QdrantVectorStore):
    """The real adapter over a shared in-memory client; close keeps the client."""

    client: AsyncQdrantClient

    def __init__(self, *a, **k) -> None:
        super().__init__(client=_MemStore.client, collection=k.get("collection"))

    async def close(self) -> None:
        return None


async def _mem(create: bool = True) -> AsyncQdrantClient:
    client = AsyncQdrantClient(location=":memory:")
    if create:
        for name in (MM, "knowledge"):  # multimodal + the (empty) text collection
            await client.create_collection(
                name, vectors_config=VectorParams(size=2, distance=Distance.COSINE)
            )
    _MemStore.client = client
    return client


async def _put(client, ws, doc, ver, page) -> None:
    await client.upsert(
        MM,
        points=[
            PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ws}:{doc}:{ver}:{page}")),
                vector=[1.0, 0.5],
                payload={
                    "workspace_id": str(ws),
                    "document_id": str(doc),
                    "document_version_id": str(ver),
                },
            )
        ],
    )


async def _left(client) -> set[tuple[str, str, str]]:
    points, _ = await client.scroll(MM, limit=1000, with_payload=True)
    return {
        (p.payload["workspace_id"], p.payload["document_id"], p.payload["document_version_id"])
        for p in points
    }


@pytest.fixture
def mem(monkeypatch):
    monkeypatch.setattr("retrieval.adapters.qdrant_store.QdrantVectorStore", _MemStore)


# --- the shared version-scoped delete (real in-memory Qdrant) ---------------


def test_delete_is_scoped_by_workspace_document_and_version(mem) -> None:
    async def go():
        client = await _mem()
        for ws, doc, ver in [(WS, DOC, V1), (WS, DOC, V2), (WS2, DOC, V1), (WS, DOC2, V1)]:
            await _put(client, ws, doc, ver, 1)
        removed = await dedup.delete_multimodal_versions(
            workspace_id=WS, document_id=DOC, version_ids=[V1]
        )
        return removed, await _left(client)

    removed, left = asyncio.run(go())
    assert removed == 1
    assert left == {
        (str(WS), str(DOC), str(V2)),
        (str(WS2), str(DOC), str(V1)),  # other tenant, same ids: untouched
        (str(WS), str(DOC2), str(V1)),  # other document, same version id: untouched
    }


def test_missing_collection_is_a_noop(mem) -> None:
    async def go():
        await _mem(create=False)
        return await dedup.delete_multimodal_versions(
            workspace_id=WS, document_id=DOC, version_ids=[V1]
        )

    assert asyncio.run(go()) == 0


# --- post-publish step: liveness re-check ------------------------------------


def _step_env(monkeypatch, *, reason, during_step=None, step_error=None):
    """Fake step writes this version's page into the real memory store."""
    calls: list[str] = []

    async def step(content, *, job_id, workspace_id, document_id, version_id):
        calls.append("step")
        if during_step is not None:
            await during_step()
        if step_error is not None:
            raise step_error
        await _put(_MemStore.client, workspace_id, document_id, version_id, 1)
        return 1

    async def state(session, *, document_id, version_id):
        calls.append("recheck")
        if isinstance(reason, Exception):
            raise reason
        return reason

    monkeypatch.setattr(ip, "_run_multimodal_step", step)
    monkeypatch.setattr(ip, "generation_publication_state", state)
    return calls


class _Sess:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _after_publish(session=None, version_id=V1):
    return ip._multimodal_after_publish(
        session or _Sess(),
        b"%PDF",
        job_id=uuid.uuid4(),
        workspace_id=WS,
        document_id=DOC,
        version_id=version_id,
    )


def test_delete_landing_during_the_step_leaves_no_points(monkeypatch, mem, caplog) -> None:
    async def purge_document_now():  # purge_document's multimodal delete, before the upsert
        await _MemStore(collection=MM).delete_if_exists(
            collection=MM, query_filter=dedup.build_purge_filter(WS, DOC)
        )

    _step_env(monkeypatch, reason=ip.FENCE_DELETED, during_step=purge_document_now)

    async def go():
        client = await _mem()
        await _put(client, WS, DOC, V1, 2)
        with caplog.at_level(logging.WARNING, logger=ip.logger.name):
            await _after_publish()
        return await _left(client)

    assert asyncio.run(go()) == set()
    events = {getattr(r, "event", "") for r in caplog.records}
    assert "multimodal_dead_generation_cleaned" in events


def test_replace_landing_during_the_step_keeps_only_the_new_version(monkeypatch, mem) -> None:
    _step_env(monkeypatch, reason=ip.FENCE_SUPERSEDED)

    async def go():
        client = await _mem()
        await _put(client, WS, DOC, V2, 1)  # the replacement already published
        await _put(client, WS2, DOC, V1, 1)  # other tenant, same ids
        await _after_publish(version_id=V1)
        return await _left(client)

    assert asyncio.run(go()) == {(str(WS), str(DOC), str(V2)), (str(WS2), str(DOC), str(V1))}


def test_live_generation_keeps_its_points(monkeypatch, mem) -> None:
    calls = _step_env(monkeypatch, reason=None)
    sess = _Sess()

    async def go():
        client = await _mem()
        await _after_publish(sess)
        return await _left(client)

    assert asyncio.run(go()) == {(str(WS), str(DOC), str(V1))}
    assert calls == ["step", "recheck"]
    assert sess.commits == 1  # the recheck's read transaction is closed, not left idle


def test_failures_after_ready_never_raise(monkeypatch, mem, caplog) -> None:
    _step_env(monkeypatch, reason=RuntimeError("db gone"))

    async def go():
        await _mem()
        with caplog.at_level(logging.WARNING, logger=ip.logger.name):
            await _after_publish()  # must not raise: the job is already READY

    asyncio.run(go())
    assert any(
        getattr(r, "event", "") == "multimodal_dead_generation_cleanup_failed"
        for r in caplog.records
    )


def test_step_runs_only_after_the_ready_commit() -> None:
    source = inspect.getsource(ip.process_job)
    assert "_run_multimodal_step(" not in source
    call = source.index("await _multimodal_after_publish(")
    assert call > source.index("publish_job_if_generation_live(")
    assert call > source.rindex("await session.commit()", 0, call)
    assert source.count("await session.commit()", call) == 0


# --- fence unpublish ---------------------------------------------------------


class _UnpubSession:
    def __init__(self, workspace_id) -> None:
        self.workspace_id = workspace_id
        self.commits = 0

    async def execute(self, *a, **k):
        return None

    async def scalar(self, *a, **k):
        return self.workspace_id

    async def commit(self) -> None:
        self.commits += 1


def test_unpublish_removes_the_versions_multimodal_points(mem, monkeypatch) -> None:
    monkeypatch.setattr(ip, "vector_collection", lambda: "knowledge")

    async def go():
        client = await _mem()
        for ws, doc, ver in [(WS, DOC, V1), (WS, DOC, V2), (WS2, DOC, V1)]:
            await _put(client, ws, doc, ver, 1)
        sess = _UnpubSession(WS)
        await ip._unpublish_generation(sess, document_id=DOC, version_id=V1)
        return sess, await _left(client)

    sess, left = asyncio.run(go())
    assert left == {(str(WS), str(DOC), str(V2)), (str(WS2), str(DOC), str(V1))}
    assert sess.commits == 1


class _FailingMM:
    calls: ClassVar[list[str]] = []

    def __init__(self, *a, collection=None, **k) -> None:
        self.collection = collection

    async def delete_versions(self, *, collection, version_ids, document_id=None, **k):
        _FailingMM.calls.append(collection)
        if collection == MM:
            raise VectorStoreError("qdrant down")
        return 0

    async def close(self) -> None:
        return None


def test_unpublish_multimodal_error_is_logged_like_the_text_cleanup(monkeypatch, caplog) -> None:
    _FailingMM.calls = []
    monkeypatch.setattr("retrieval.adapters.qdrant_store.QdrantVectorStore", _FailingMM)
    monkeypatch.setattr(ip, "vector_collection", lambda: "knowledge")
    sess = _UnpubSession(WS)
    with caplog.at_level(logging.WARNING, logger=ip.logger.name):
        asyncio.run(ip._unpublish_generation(sess, document_id=DOC, version_id=V1))
    assert _FailingMM.calls == ["knowledge", MM]
    event = "fenced_generation_vector_cleanup_failed"
    failed = [r for r in caplog.records if getattr(r, "event", "") == event]
    assert len(failed) == 1 and "qdrant down" in failed[0].error
    assert sess.commits == 1  # still commits the Postgres unpublish, like the text path


# --- supersede (flag-independent) --------------------------------------------


def test_supersede_removes_old_versions_multimodal_points(mem, monkeypatch) -> None:
    monkeypatch.setattr(dedup, "vector_collection", lambda: "knowledge")
    monkeypatch.setattr(ip, "MULTIMODAL_EMBED_ENABLED", False)

    async def go():
        client = await _mem()
        for ws, doc, ver in [(WS, DOC, V1), (WS, DOC, V2), (WS2, DOC, V1)]:
            await _put(client, ws, doc, ver, 1)
        removed = await dedup._delete_superseded_points(
            document_id=DOC, version_ids=[V1], workspace_id=WS
        )
        return removed, await _left(client)

    removed, left = asyncio.run(go())
    assert removed == 1
    assert left == {(str(WS), str(DOC), str(V2)), (str(WS2), str(DOC), str(V1))}


def test_supersede_passes_the_workspace_to_both_cleanups() -> None:
    source = inspect.getsource(dedup.supersede_other_versions)
    assert "workspace_id=workspace_id" in source
    assert source.count("workspace_id=workspace_id") >= 2  # targeted cleanup + reconcile


class _MMOutage:
    attempts: ClassVar[list[str]] = []

    def __init__(self, *a, **k) -> None:
        pass

    async def delete_versions(self, *, collection, version_ids, document_id=None, **k):
        _MMOutage.attempts.append(collection)
        if k.get("workspace_id") is not None:
            raise VectorStoreError("qdrant down")
        return 0

    async def close(self) -> None:
        return None


def test_supersede_multimodal_error_takes_the_text_failure_path(monkeypatch) -> None:
    """Same retry budget, same marker + queued retry as a text-cleanup failure."""
    import ingestion.queue as queue_mod

    _MMOutage.attempts = []
    monkeypatch.setattr("retrieval.adapters.qdrant_store.QdrantVectorStore", _MMOutage)
    monkeypatch.setattr(dedup, "vector_collection", lambda: "knowledge")

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(dedup.asyncio, "sleep", _no_sleep)
    scheduled: list[str] = []

    async def _capture(job_id, delay):
        scheduled.append(job_id)

    monkeypatch.setattr(queue_mod, "schedule_retry", _capture)

    class _Job:
        id = uuid.uuid4()
        attempts, max_attempts = 1, 3
        error_code = error_message = None

    job = _Job()

    async def _latest(session, version_id):
        return job

    monkeypatch.setattr(dedup, "latest_job_for_version", _latest)
    ok = asyncio.run(
        dedup._cleanup_superseded_vectors(
            None, document_id=DOC, version_ids=[V1], keep_version_id=V2, workspace_id=WS
        )
    )
    assert ok is False
    assert _MMOutage.attempts.count(MM) == dedup._VECTOR_CLEANUP_ATTEMPTS
    assert job.error_code == "SUPERSESSION_CLEANUP_RETRY"
    assert scheduled == [str(job.id)]
