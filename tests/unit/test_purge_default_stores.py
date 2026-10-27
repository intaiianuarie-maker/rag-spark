"""T-V1-058 unit: the originals purger must not shadow the default stores.

T-055 added the ``originals`` store purger and registered it **before** the
``if not _STORE_PURGERS`` block that lazily registers fts/qdrant. On a process
that had not purged yet, that made the registry non-empty, so fts and qdrant
were never registered at all: the first purge reported
``{'postgres': True, 'originals': True}`` — a clean 204 — while the document's
``lexical_docs`` row and its Qdrant point survived. The integration test only
caught it once T-V1-043 put real embeddings back online and the Qdrant
precondition stopped failing first.

So the invariant under test is registration ORDER, not behaviour: whatever a
purge registers for one document must never prevent the default stores from
being registered. Two proofs here:

1. a spy on ``register_store_purger`` records the order on a virgin registry;
2. the real loop runs with fakes patched in at the adapter seam, so the report
   proves all three stores were purged (and that the deletes are
   **document**-scoped, which is the other half of the ticket's question).

Service-free by design: no Qdrant, no Postgres, no object store — the store
adapters are replaced at the modules the purge path imports them from.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar, cast

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion.dedup import StorePurger, purge_document, registered_stores
from retrieval.interfaces import SearchFilter
from storage.interfaces import StorageAdapter, StorageObject
from storage.originals import put_original, sha256_hex

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.unit

PAYLOAD = b"T-V1-058 regression payload\r\n\x00\x01\x02"


class FakeStore(StorageAdapter):
    """In-memory object store — enough for the originals half of the purge."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.listed: list[str] = []
        self.deleted: list[str] = []

    async def put_object(self, bucket, key, data, metadata=None) -> StorageObject:
        self.objects[(bucket, key)] = bytes(data)
        return StorageObject(key=key, size_bytes=len(data), etag="fake")

    async def get_object(self, bucket: str, key: str) -> bytes:
        if (bucket, key) not in self.objects:
            raise KeyError(f"absent: {key}")
        return self.objects[(bucket, key)]

    async def delete_object(self, bucket: str, key: str) -> bool:
        self.deleted.append(key)
        return self.objects.pop((bucket, key), None) is not None

    async def presign_url(self, bucket, key, operation="get", expiry_seconds=3600) -> str:
        return f"http://example.invalid/{bucket}/{key}"

    async def list_objects(self, bucket, prefix="", max_keys=1000) -> list[StorageObject]:
        self.listed.append(prefix)
        return [
            StorageObject(key=key, size_bytes=len(data))
            for (bucket_name, key), data in sorted(self.objects.items())
            if bucket_name == bucket and key.startswith(prefix)
        ]


class FakeLexicalStore:
    """Stands in for ``PostgresLexicalStore`` — records the filter it deletes with."""

    calls: ClassVar[list[SearchFilter]] = []

    async def delete(self, *, ids=None, query_filter=None) -> int:
        FakeLexicalStore.calls.append(query_filter)
        return 1


class FakeVectorStore:
    """Stands in for ``QdrantVectorStore`` — records the filter it deletes with."""

    calls: ClassVar[list[tuple[str, SearchFilter]]] = []
    closed = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def delete(self, *, collection=None, query_filter=None) -> None:
        FakeVectorStore.calls.append((collection, query_filter))

    async def close(self) -> None:
        FakeVectorStore.closed += 1


class _Scalars:
    def __init__(self, rows: Sequence[object]) -> None:
        self._rows = list(rows)

    def all(self) -> list[object]:
        return list(self._rows)


class _Result:
    def __init__(self, rows: Sequence[object]) -> None:
        self._rows = list(rows)

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)


class FakeDocument:
    def __init__(self, workspace_id: uuid.UUID) -> None:
        self.workspace_id = workspace_id
        self.deleted_at = None


class FakeSession:
    """The smallest session ``purge_document`` accepts (T-V1-055's shape)."""

    def __init__(
        self,
        document: FakeDocument,
        version_ids: Sequence[uuid.UUID],
        storage_keys: Sequence[str | None],
    ) -> None:
        self.document = document
        self.version_ids = list(version_ids)
        self.storage_keys = list(storage_keys)
        self.commits = 0

    async def get(self, *args: object, **kwargs: object) -> FakeDocument:
        return self.document

    async def execute(self, statement: object) -> _Result:
        sql = str(statement)
        if "storage_key" in sql:
            return _Result(self.storage_keys)
        if sql.lstrip().upper().startswith("DELETE"):
            return _Result([])
        return _Result(self.version_ids)

    async def commit(self) -> None:
        self.commits += 1


async def _stored_key(store: StorageAdapter, workspace_id, document_id, version_id) -> str:
    return await put_original(
        workspace_id=workspace_id,
        document_id=document_id,
        version_id=version_id,
        data=PAYLOAD,
        checksum=sha256_hex(PAYLOAD),
        filename="probe.txt",
        adapter=store,
    )


@pytest.fixture
def purgers():
    """A registry owned by the test, restored afterwards (module-level state)."""
    from ingestion import dedup

    saved = dict(dedup._STORE_PURGERS)
    dedup._STORE_PURGERS.clear()
    yield dedup._STORE_PURGERS
    dedup._STORE_PURGERS.clear()
    dedup._STORE_PURGERS.update(saved)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fake = FakeStore()
    monkeypatch.setattr("storage.originals.get_adapter", lambda: fake)
    return fake


def _session(workspace_id, version_ids, storage_keys) -> FakeSession:
    return FakeSession(FakeDocument(workspace_id), version_ids, storage_keys)


async def _purge(
    session: FakeSession, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> dict[str, bool]:
    """The real ``purge_document``, called with a double the type checker takes."""
    return await purge_document(
        cast("AsyncSession", session), workspace_id=workspace_id, document_id=document_id
    )


# --- registration order: the T-V1-058 invariant -----------------------------


async def test_originals_is_registered_after_the_default_stores(
    monkeypatch: pytest.MonkeyPatch, purgers, store
) -> None:
    """On a virgin registry the default stores are registered and not shadowed.

    The spy replaces the registry, so nothing is registered and nothing runs —
    the test asserts the ORDER of registration, which is the only thing that
    was wrong in T-055. A record-and-register spy would also let the real
    fts/qdrant purgers reach Postgres and Qdrant from a unit test.
    """
    from ingestion import dedup

    recorded: list[str] = []

    def _spy(name: str, purger: StorePurger) -> None:
        recorded.append(name)

    monkeypatch.setattr(dedup, "register_store_purger", _spy)
    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()
    version_id = uuid.uuid4()
    key = await _stored_key(store, workspace_id, document_id, version_id)

    report = await _purge(_session(workspace_id, [version_id], [key]), workspace_id, document_id)

    assert recorded == ["fts", "qdrant", "originals"], recorded
    assert recorded.index("originals") > recorded.index("fts")
    assert recorded.index("originals") > recorded.index("qdrant")
    # Nothing was registered, so nothing ran: the report stays postgres-only
    # and no unit test reaches a live store.
    assert report == {"postgres": True}
    assert store.deleted == [] and store.listed == []


async def test_a_version_less_purge_registers_the_defaults_and_no_originals(
    monkeypatch: pytest.MonkeyPatch, purgers, store
) -> None:
    """No version rows: defaults registered, and no originals purger added."""
    from ingestion import dedup

    recorded: list[str] = []

    def _spy(name: str, purger: StorePurger) -> None:
        recorded.append(name)

    monkeypatch.setattr(dedup, "register_store_purger", _spy)
    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()

    report = await _purge(_session(workspace_id, [], []), workspace_id, document_id)

    assert recorded == ["fts", "qdrant"]
    assert "originals" not in purgers
    assert report == {"postgres": True}
    assert store.deleted == [] and store.listed == []


async def test_a_version_less_purge_drops_a_stale_originals_purger(purgers, store) -> None:
    """A purger left registered by an earlier document must not fire here."""
    fired: list[str] = []

    async def stale(ws_id: uuid.UUID, doc_id: uuid.UUID) -> None:
        fired.append(str(doc_id))

    purgers["originals"] = stale
    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()

    report = await _purge(_session(workspace_id, [], []), workspace_id, document_id)

    assert "originals" not in purgers
    assert fired == []
    assert "originals" not in report


# --- behaviour: all three stores really purge, document-scoped --------------


async def test_a_first_purge_reaches_all_three_stores_and_scopes_by_document(
    monkeypatch: pytest.MonkeyPatch, purgers, store
) -> None:
    """The report names every store, and every delete carries the document scope.

    ``fts``/``qdrant`` are patched at the modules the purge imports them from,
    so this exercises the real loop — a 204-shaped report with the lexical and
    vector halves missing is exactly what T-V1-058 shipped by accident.
    """
    FakeLexicalStore.calls.clear()
    FakeVectorStore.calls.clear()
    FakeVectorStore.closed = 0
    monkeypatch.setattr("retrieval.adapters.pg_fts.PostgresLexicalStore", FakeLexicalStore)
    monkeypatch.setattr("retrieval.adapters.qdrant_store.QdrantVectorStore", FakeVectorStore)
    # Hermetic collection name: integration modules export VECTOR_COLLECTION at
    # import time (scratch names), and a full ``-m unit`` collection imports
    # them all. Production never exports it and keeps the default.
    monkeypatch.setattr("ingestion.dedup.vector_collection", lambda: "knowledge")

    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()
    version_id = uuid.uuid4()
    key = await _stored_key(store, workspace_id, document_id, version_id)
    session = _session(workspace_id, [version_id], [key])

    report = await _purge(session, workspace_id, document_id)

    assert report == {"postgres": True, "fts": True, "qdrant": True, "originals": True}
    assert set(registered_stores()) == {"fts", "qdrant", "originals"}
    # Document-scoped, not version-scoped: the filter carries the document.
    assert len(FakeLexicalStore.calls) == 1
    lexical_filter = FakeLexicalStore.calls[0]
    assert lexical_filter.document_ids == [str(document_id)]
    assert lexical_filter.workspace_ids == [str(workspace_id)]
    assert len(FakeVectorStore.calls) == 1
    vector_collection, vector_filter = FakeVectorStore.calls[0]
    assert vector_collection == "knowledge"
    assert vector_filter.document_ids == [str(document_id)]
    assert vector_filter.workspace_ids == [str(workspace_id)]
    assert FakeVectorStore.closed == 1
    # And the originals really left the store.
    assert store.deleted == [key]
    assert store.objects == {}
    assert session.commits == 1
    assert session.document.deleted_at is not None


async def test_a_second_purge_in_the_same_process_still_purges_every_store(
    monkeypatch: pytest.MonkeyPatch, purgers, store
) -> None:
    """The registry persists across purges — the default stores stay registered.

    This is the state the API process is in after the first delete, and the
    state in which T-V1-058's survivor appeared.
    """
    FakeLexicalStore.calls.clear()
    FakeVectorStore.calls.clear()
    monkeypatch.setattr("retrieval.adapters.pg_fts.PostgresLexicalStore", FakeLexicalStore)
    monkeypatch.setattr("retrieval.adapters.qdrant_store.QdrantVectorStore", FakeVectorStore)

    workspace_id = uuid.uuid4()
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    first_version, second_version = uuid.uuid4(), uuid.uuid4()
    first_key = await _stored_key(store, workspace_id, first_id, first_version)
    second_key = await _stored_key(store, workspace_id, second_id, second_version)

    first = await _purge(
        _session(workspace_id, [first_version], [first_key]), workspace_id, first_id
    )
    store.deleted.clear()
    second = await _purge(
        _session(workspace_id, [second_version], [second_key]), workspace_id, second_id
    )

    assert first == {"postgres": True, "fts": True, "qdrant": True, "originals": True}
    assert second == first
    assert len(FakeLexicalStore.calls) == 2  # one per purge, never skipped
    assert len(FakeVectorStore.calls) == 2
    assert store.deleted == [second_key]
