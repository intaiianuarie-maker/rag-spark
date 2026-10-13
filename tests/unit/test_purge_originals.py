"""T-V1-055 unit: purging a document deletes its originals from object storage.

T-039 moved the original bytes out of ``document_versions.content`` into the
object store and its own handoff flagged the consequence as risk 1: deleting a
document only tombstoned rows, so the bytes stayed retrievable forever — a
privacy/retention hole, not a latency one. This file is the follow-up's
evidence.

Service-free by design (unit tests must run with compose down): the object store
is an in-memory ``StorageAdapter`` behind the same seam the product uses, and the
session is the smallest double ``purge_document`` accepts — so these assertions
are about *our* rules (which keys go, when, and what a failed delete does to the
per-store report), never about a live S3.

The failure-honesty contract under test is the one the endpoint already had: the
report stays ``dict[str, bool]`` keyed by store name, and a store that could not
be purged is ``False`` there. Adding originals must not invent a new shape, a new
return type, or a silent success.

The live half — real SeaweedFS, real ``put_original`` keys, no database — is the
``integration``-marked test at the bottom; it skips loudly (naming the command
that starts the store) when nothing is listening, the way
``tests/fixtures/object_store_env.py`` does.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, cast

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion.dedup import (
    StorePurger,
    delete_document_originals,
    originals_prefix,
    purge_document,
    register_store_purger,
)
from storage.interfaces import StorageAdapter, StorageError, StorageObject
from storage.originals import (
    originals_bucket,
    originals_key,
    put_original,
    sha256_hex,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.unit

#: Two payloads that show transport/encoding damage (NULs, high bytes, CRLF).
PAYLOADS = [b"T-V1-055 original v1\r\n\x00\x01\x02", b"original v2 tail \xff\xfe\xe2"]


class FakeStore(StorageAdapter):
    """In-memory object store with knobs for the two failure cases."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.deleted: list[str] = []
        self.listed: list[str] = []
        self.fail_deletes = False
        self.fail_list = False

    async def put_object(self, bucket: str, key: str, data: bytes, metadata=None) -> StorageObject:
        self.objects[(bucket, key)] = bytes(data)
        return StorageObject(key=key, size_bytes=len(data), etag="fake")

    async def get_object(self, bucket: str, key: str) -> bytes:
        if (bucket, key) not in self.objects:
            raise StorageError(f"get_object failed: NoSuchKey for {key}")
        return self.objects[(bucket, key)]

    async def delete_object(self, bucket: str, key: str) -> bool:
        if self.fail_deletes:
            raise StorageError("delete_object failed: store unreachable")
        self.deleted.append(key)
        return self.objects.pop((bucket, key), None) is not None

    async def presign_url(
        self, bucket: str, key: str, operation: str = "get", expiry_seconds: int = 3600
    ) -> str:
        return f"http://example.invalid/{bucket}/{key}"

    async def list_objects(
        self, bucket: str, prefix: str = "", max_keys: int = 1000
    ) -> list[StorageObject]:
        if self.fail_list:
            raise StorageError("list_objects failed: store unreachable")
        self.listed.append(prefix)
        return [
            StorageObject(key=key, size_bytes=len(data))
            for (bucket_name, key), data in sorted(self.objects.items())
            if bucket_name == bucket and key.startswith(prefix)
        ]


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
        self.deleted_at: datetime | None = None


class FakeSession:
    """The smallest session ``purge_document`` uses: two reads, one delete, one commit.

    Deliberately dispatches on the compiled SQL so the two version reads (ids,
    then storage keys) are distinguishable without a database.
    """

    def __init__(
        self,
        document: FakeDocument,
        version_ids: Sequence[uuid.UUID],
        storage_keys: Sequence[str | None],
    ) -> None:
        self.document = document
        self.version_ids = list(version_ids)
        self.storage_keys = list(storage_keys)
        self.statements: list[str] = []
        self.commits = 0
        self.commit_error: Exception | None = None

    async def get(self, *args: object, **kwargs: object) -> FakeDocument:
        return self.document

    async def execute(self, statement: object) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        if "storage_key" in sql:
            return _Result(self.storage_keys)
        if sql.lstrip().upper().startswith("DELETE"):
            return _Result([])
        return _Result(self.version_ids)

    async def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error


@pytest.fixture
def purgers() -> Iterator[dict[str, StorePurger]]:
    """A purger registry holding only no-op fts/qdrant doubles.

    Pre-populating is what keeps this a unit test: with an empty registry
    ``purge_document`` lazily registers the *real* Qdrant/Postgres purgers, and
    a unit test must not reach either. The live registry is yielded, so a test
    can assert what ended up registered, and the process-wide one is restored
    afterwards (the registry is module state shared with every other test).
    """
    from ingestion import dedup

    async def _noop(ws_id: uuid.UUID, doc_id: uuid.UUID) -> None:
        return None

    saved = dict(dedup._STORE_PURGERS)
    dedup._STORE_PURGERS.clear()
    dedup.register_store_purger("fts", _noop)
    dedup.register_store_purger("qdrant", _noop)
    yield dedup._STORE_PURGERS
    dedup._STORE_PURGERS.clear()
    dedup._STORE_PURGERS.update(saved)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """The object store the purge path talks to, and the knobs to break it."""
    fake = FakeStore()
    monkeypatch.setattr("storage.originals.get_adapter", lambda: fake)
    return fake


async def _stored(
    store: StorageAdapter,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    version_ids: Sequence[uuid.UUID],
) -> list[str]:
    """Write every version's original the way the upload path does."""
    keys = []
    for index, version_id in enumerate(version_ids):
        payload = PAYLOADS[index % len(PAYLOADS)]
        keys.append(
            await put_original(
                workspace_id=workspace_id,
                document_id=document_id,
                version_id=version_id,
                data=payload,
                checksum=sha256_hex(payload),
                filename="report.pdf",
                adapter=store,
            )
        )
    return keys


def _session(
    workspace_id: uuid.UUID,
    version_ids: Sequence[uuid.UUID],
    storage_keys: Sequence[str | None],
) -> FakeSession:
    return FakeSession(FakeDocument(workspace_id), version_ids, storage_keys)


async def _purge(
    session: FakeSession, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> dict[str, bool]:
    """The real ``purge_document``, called with a double the type checker takes."""
    return await purge_document(
        cast("AsyncSession", session), workspace_id=workspace_id, document_id=document_id
    )


# --- the fix itself -------------------------------------------------------


async def test_purge_deletes_every_versions_original(purgers, store) -> None:
    """Multi-version document: all of its originals leave the store."""
    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()
    version_ids = [uuid.uuid4() for _ in PAYLOADS]
    keys = await _stored(store, workspace_id, document_id, version_ids)
    session = _session(workspace_id, version_ids, keys)

    report = await _purge(session, workspace_id=workspace_id, document_id=document_id)

    # Exact report shape: postgres + the two registered stores + originals. No
    # new key, no new value type — the endpoint's 204/207 logic reads it as-is.
    assert report == {"postgres": True, "fts": True, "qdrant": True, "originals": True}
    assert "originals" in purgers  # registered through the same seam as fts/qdrant
    # Every version's object is gone, and gone by its exact recorded key.
    assert store.deleted == keys
    assert store.objects == {}
    # The tombstone itself is unchanged: one commit, deleted_at set.
    assert session.commits == 1
    assert session.document.deleted_at is not None


async def test_purge_deletes_a_legacy_original_the_row_never_keyed(purgers, store) -> None:
    """A version row predating ``storage_key`` keeps an object under its prefix.

    That object's key cannot be reconstructed from Postgres (the extension came
    from a filename no longer recorded anywhere), so the prefix sweep is the
    only thing that can find it — and it does.
    """
    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()
    keyed_version, legacy_version = uuid.uuid4(), uuid.uuid4()
    keys = await _stored(store, workspace_id, document_id, [keyed_version])
    legacy_key = f"{originals_prefix(workspace_id, document_id)}{legacy_version}.pdf"
    store.deleted.clear()
    await store.put_object(originals_bucket(), legacy_key, b"pre-0009 bytes")

    # The row for the legacy version records no key at all.
    session = _session(workspace_id, [keyed_version, legacy_version], [keys[0], None])
    report = await _purge(session, workspace_id=workspace_id, document_id=document_id)

    assert report["originals"] is True
    assert sorted(store.deleted) == sorted([keys[0], legacy_key])
    assert store.objects == {}


async def test_purge_reports_originals_false_when_the_delete_fails(purgers, store) -> None:
    """Failure-honest: a store that refuses the delete is not a clean purge."""
    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()
    version_ids = [uuid.uuid4() for _ in PAYLOADS]
    keys = await _stored(store, workspace_id, document_id, version_ids)
    store.fail_deletes = True
    session = _session(workspace_id, version_ids, keys)

    report = await _purge(session, workspace_id=workspace_id, document_id=document_id)

    assert report == {"postgres": True, "fts": True, "qdrant": True, "originals": False}
    assert [name for name, ok in report.items() if not ok] == ["originals"]
    # The bytes are still there — which is exactly why the report says so.
    assert sorted(store.objects) == sorted((originals_bucket(), key) for key in keys)
    # And the tombstone still stands: a degraded purge deletes nothing new.
    assert session.document.deleted_at is not None
    assert session.commits == 1


async def test_purge_reports_originals_false_when_the_store_cannot_be_listed(
    purgers, store
) -> None:
    """The sweep is a safety net, so its failure is a purge failure too."""
    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()
    version_ids = [uuid.uuid4() for _ in PAYLOADS]
    keys = await _stored(store, workspace_id, document_id, version_ids)
    store.fail_list = True
    session = _session(workspace_id, version_ids, keys)

    report = await _purge(session, workspace_id=workspace_id, document_id=document_id)

    assert report["originals"] is False
    assert store.deleted == []  # nothing was even attempted


async def test_purge_never_touches_another_documents_originals(purgers, store) -> None:
    """Prefix- and key-scoped: a sibling document in the same workspace survives."""
    workspace_id = uuid.uuid4()
    doomed_id, survivor_id = uuid.uuid4(), uuid.uuid4()
    doomed_keys = await _stored(store, workspace_id, doomed_id, [uuid.uuid4()])
    survivor_keys = await _stored(store, workspace_id, survivor_id, [uuid.uuid4()])
    survivor_prefix = originals_prefix(workspace_id, survivor_id)

    session = _session(workspace_id, [uuid.uuid4()], doomed_keys)
    report = await _purge(session, workspace_id=workspace_id, document_id=doomed_id)

    assert report["originals"] is True
    assert store.deleted == doomed_keys
    assert store.listed == [originals_prefix(workspace_id, doomed_id)]
    assert [key for (_bucket, key) in store.objects] == survivor_keys
    assert survivor_prefix not in store.listed


async def test_originals_leave_after_the_tombstone_commit(purgers, store) -> None:
    """Ordering: a commit that fails can never destroy bytes for a live document."""
    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()
    version_ids = [uuid.uuid4() for _ in PAYLOADS]
    keys = await _stored(store, workspace_id, document_id, version_ids)
    session = _session(workspace_id, version_ids, keys)
    session.commit_error = RuntimeError("postgres gone")

    with pytest.raises(RuntimeError, match="postgres gone"):
        await _purge(session, workspace_id=workspace_id, document_id=document_id)

    assert session.commits == 1
    assert store.deleted == [] and store.listed == []
    assert sorted(store.objects) == sorted((originals_bucket(), key) for key in keys)


async def test_purge_without_versions_never_reaches_the_store(purgers, store) -> None:
    """No version rows: no originals entry, no store call, no stale purger."""
    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()
    # A purger left registered by an earlier document must not fire here.
    register_store_purger("originals", _raise_purger)
    session = _session(workspace_id, [], [])

    report = await _purge(session, workspace_id=workspace_id, document_id=document_id)

    assert report == {"postgres": True, "fts": True, "qdrant": True}
    assert "originals" not in purgers
    assert store.deleted == [] and store.listed == []


async def _raise_purger(ws_id: uuid.UUID, doc_id: uuid.UUID) -> None:
    raise AssertionError("a version-less purge must not run another document's purger")


async def test_each_purge_deletes_the_keys_it_read_for_that_document(purgers, store) -> None:
    """The purger is re-registered per call, so it can never act on stale keys."""
    workspace_id = uuid.uuid4()
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    first_keys = await _stored(store, workspace_id, first_id, [uuid.uuid4()])
    second_keys = await _stored(store, workspace_id, second_id, [uuid.uuid4()])
    store.deleted.clear()

    first = await _purge(
        _session(workspace_id, [uuid.uuid4()], first_keys),
        workspace_id=workspace_id,
        document_id=first_id,
    )
    second = await _purge(
        _session(workspace_id, [uuid.uuid4()], second_keys),
        workspace_id=workspace_id,
        document_id=second_id,
    )

    assert (first["originals"], second["originals"]) == (True, True)
    # Exactly each document's own keys — no re-delete of the first document's.
    assert store.deleted == [*first_keys, *second_keys]


# --- the operation, on its own --------------------------------------------


async def test_delete_document_originals_dedupes_keys_and_counts_what_went(store) -> None:
    """The count is objects actually removed, and a repeated key is one delete."""
    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()
    kept = originals_key(workspace_id, document_id, uuid.uuid4(), "a.pdf")
    absent = originals_key(workspace_id, document_id, uuid.uuid4(), "b.pdf")
    await store.put_object(originals_bucket(), kept, b"present")

    removed = await delete_document_originals(
        workspace_id=workspace_id, document_id=document_id, storage_keys=[kept, absent, kept]
    )

    assert removed == 1
    assert store.deleted == [kept, absent]  # absent object: one delete, nothing removed
    assert store.objects == {}


async def test_delete_document_originals_with_nothing_stored_is_a_clean_no_op(store) -> None:
    """A document that never had bytes (the EMPTY_DOCUMENT row) is not a failure."""
    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()

    removed = await delete_document_originals(
        workspace_id=workspace_id, document_id=document_id, storage_keys=[]
    )

    assert removed == 0
    assert store.listed == [originals_prefix(workspace_id, document_id)]
    assert store.deleted == []


def test_originals_prefix_is_one_documents_objects_and_nobody_elses() -> None:
    """The sweep's safety property: the prefix cannot reach another document."""
    workspace_id = uuid.uuid4()
    first, second = uuid.uuid4(), uuid.uuid4()
    prefix = originals_prefix(workspace_id, first)

    assert prefix.startswith("originals/")
    assert prefix.endswith(f"/{first}/")
    others = (originals_prefix(workspace_id, second), originals_prefix(uuid.uuid4(), first))
    assert prefix not in others
    # The keys the upload path writes really do live under this prefix.
    key = originals_key(workspace_id, first, uuid.uuid4(), "report.pdf")
    assert key.startswith(prefix)


# --- live store (self-skips when SeaweedFS is down) ------------------------


@pytest.mark.integration
async def test_live_purge_removes_the_originals_from_the_real_store(purgers) -> None:
    """Real SeaweedFS, real keys: after the purge the bytes are not retrievable.

    No database: the session double carries the rows, the store is the real
    adapter behind the seam — which is what proves the shipped path (endpoint,
    credentials, bucket, ``get_adapter``) deletes the object, not just a fake.
    """
    from object_store_env import apply_if_reachable
    from storage.originals import get_adapter, load_original

    if apply_if_reachable() is None:
        pytest.skip("SeaweedFS object store not reachable; run `docker compose up -d seaweedfs`")

    live = get_adapter()
    bucket = originals_bucket()
    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()
    version_ids = [uuid.uuid4() for _ in PAYLOADS]
    keys = await _stored(live, workspace_id, document_id, version_ids)
    checksums = [sha256_hex(payload) for payload in PAYLOADS]

    # Precondition: the real store really does hold the bytes.
    assert await load_original(keys[0], checksum=checksums[0]) == PAYLOADS[0]

    session = _session(workspace_id, version_ids, keys)
    report = await _purge(session, workspace_id=workspace_id, document_id=document_id)

    assert report["originals"] is True
    assert await live.list_objects(bucket, originals_prefix(workspace_id, document_id)) == []
    for key, checksum in zip(keys, checksums, strict=True):
        with pytest.raises(StorageError):
            await load_original(key, checksum=checksum)
