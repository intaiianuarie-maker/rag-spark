"""T-V1-039 unit: originals round-trip byte-identical, and Postgres holds no bytes.

Service-free by design (unit tests must run with compose down): the object
store is an in-memory ``StorageAdapter`` behind the same seam the product
uses, so these assertions are about *our* rules — key scheme, checksum
enforcement on both sides, failure taxonomy — not about a live S3.

The byte-identity assertion here is the ticket metric (``round-trip SHA match:
yes``); the proof against the real SeaweedFS, including the live upload path
and the migration backfill, is
``tests/integration/test_originals_object_store.py``.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest
import sqlalchemy as sa

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from storage.interfaces import StorageAdapter, StorageError, StorageObject
from storage.originals import (
    ORIGINALS_PREFIX,
    OriginalChecksumMismatch,
    OriginalMissing,
    load_original,
    originals_bucket,
    originals_key,
    put_original,
    sha256_hex,
)

pytestmark = pytest.mark.unit

#: Payload that shows any transport/encoding damage: NULs, high bytes, CRLF and
#: a non-UTF8 run — exactly what a "must be text" shortcut ruins.
RAW_BYTES = b"%PDF-1.4\r\n%\xe2\xe3\xcf\xd3\n\x00\x01\x02binary-tail\xff\xfe"


class _ClientErrorStub(Exception):
    """What botocore's ``ClientError`` looks like from inside this seam.

    Must be an ``Exception``: Python only accepts exception subclasses as the
    cause, and ``storage.originals._object_absent`` reads ``response`` off it.
    """

    def __init__(self, code: str) -> None:
        super().__init__(f"An error occurred ({code})")
        self.response = {"Error": {"Code": code}}


class FakeStore(StorageAdapter):
    """In-memory object store with knobs for the failure cases."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts = 0
        self.corrupt_on_put = False
        self.unreachable = False

    async def put_object(
        self, bucket: str, key: str, data: bytes, metadata=None
    ) -> StorageObject:
        self.puts += 1
        if self.unreachable:
            raise StorageError("put_object failed: endpoint unreachable")
        stored = bytes(data)
        if self.corrupt_on_put:
            stored = stored[:-1] + bytes([stored[-1] ^ 0xFF])
        self.objects[(bucket, key)] = stored
        return StorageObject(key=key, size_bytes=len(data), etag="fake")

    async def get_object(self, bucket: str, key: str) -> bytes:
        if self.unreachable:
            raise StorageError("get_object failed: endpoint unreachable")
        if (bucket, key) not in self.objects:
            raise StorageError("get_object failed: NoSuchKey") from _ClientErrorStub(
                "NoSuchKey"
            )
        return self.objects[(bucket, key)]

    async def delete_object(self, bucket: str, key: str) -> bool:
        return self.objects.pop((bucket, key), None) is not None

    async def presign_url(
        self, bucket: str, key: str, operation: str = "get", expiry_seconds: int = 3600
    ) -> str:
        return f"http://example.invalid/{bucket}/{key}"

    async def list_objects(
        self, bucket: str, prefix: str = "", max_keys: int = 1000
    ) -> list[StorageObject]:
        return [
            StorageObject(key=key, size_bytes=len(data))
            for (bucket_name, key), data in sorted(self.objects.items())
            if bucket_name == bucket and key.startswith(prefix)
        ]


async def _store(
    store: FakeStore,
    data: bytes = RAW_BYTES,
    *,
    checksum: str | None = None,
    filename: str | None = "report.pdf",
    verify: bool = True,
) -> str:
    """Store ``data`` for a fresh (workspace, document, version) triple."""
    return await put_original(
        workspace_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        data=data,
        checksum=checksum if checksum is not None else sha256_hex(data),
        filename=filename,
        adapter=store,
        verify=verify,
    )


# --- round trip (the ticket metric) ----------------------------------------


async def test_put_then_load_is_byte_identical() -> None:
    store = FakeStore()
    key = await _store(store, RAW_BYTES)
    fetched = await load_original(key, checksum=sha256_hex(RAW_BYTES), adapter=store)
    assert fetched == RAW_BYTES
    assert sha256_hex(fetched) == sha256_hex(RAW_BYTES)


async def test_round_trip_through_a_second_store_still_matches() -> None:
    """Store -> load -> store -> load: same bytes, no drift, checksum holds."""
    first, second = FakeStore(), FakeStore()
    key = await _store(first, RAW_BYTES)
    carried = await load_original(key, checksum=sha256_hex(RAW_BYTES), adapter=first)
    key2 = await _store(second, carried)
    assert await load_original(key2, checksum=sha256_hex(RAW_BYTES), adapter=second) == RAW_BYTES


# --- checksum enforced, both directions -------------------------------------


async def test_put_refuses_bytes_that_do_not_match_the_declared_checksum() -> None:
    store = FakeStore()
    with pytest.raises(OriginalChecksumMismatch):
        await put_original(
            workspace_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            data=RAW_BYTES,
            checksum=sha256_hex(b"something else"),
            adapter=store,
        )
    assert store.puts == 0, "bytes that fail verification must never be written"


async def test_put_read_after_write_refuses_a_corrupted_write() -> None:
    store = FakeStore()
    store.corrupt_on_put = True
    with pytest.raises(OriginalChecksumMismatch):
        await _store(store, RAW_BYTES)
    assert store.puts == 1, "the write happened; the refusal is about what landed"


async def test_load_refuses_an_object_that_does_not_match_the_checksum() -> None:
    store = FakeStore()
    key = await _store(store, RAW_BYTES)
    store.objects[(originals_bucket(), key)] = b"tampered"
    with pytest.raises(OriginalChecksumMismatch):
        await load_original(key, checksum=sha256_hex(RAW_BYTES), adapter=store)


async def test_load_of_a_vanished_object_reads_as_missing() -> None:
    store = FakeStore()
    key = await _store(store, RAW_BYTES)
    del store.objects[(originals_bucket(), key)]
    with pytest.raises(OriginalMissing):
        await load_original(key, checksum=sha256_hex(RAW_BYTES), adapter=store)


async def test_load_of_an_unreachable_store_stays_a_plain_storage_error() -> None:
    """A transport blip must NOT read as "the object is gone": that would turn
    a retryable condition into a terminal verdict."""
    store = FakeStore()
    key = await _store(store, RAW_BYTES)
    store.unreachable = True
    with pytest.raises(StorageError) as excinfo:
        await load_original(key, checksum=sha256_hex(RAW_BYTES), adapter=store)
    assert not isinstance(excinfo.value, (OriginalMissing, OriginalChecksumMismatch))


async def test_load_without_a_storage_key_is_original_missing() -> None:
    with pytest.raises(OriginalMissing):
        await load_original(None, checksum=sha256_hex(RAW_BYTES), adapter=FakeStore())


async def test_put_write_failure_is_reported_as_a_storage_error() -> None:
    store = FakeStore()
    store.unreachable = True
    with pytest.raises(StorageError):
        await put_original(
            workspace_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            data=RAW_BYTES,
            checksum=sha256_hex(RAW_BYTES),
            adapter=store,
        )


# --- key scheme -------------------------------------------------------------


def test_key_is_deterministic_and_version_scoped() -> None:
    workspace_id, document_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    first = originals_key(workspace_id, document_id, version_id, "a.txt")
    assert first == originals_key(workspace_id, document_id, version_id, "a.txt")
    # Two versions of one document never share an object.
    assert first != originals_key(workspace_id, document_id, uuid.uuid4(), "a.txt")
    # Another tenant's workspace never maps onto this document's objects.
    assert first != originals_key(uuid.uuid4(), document_id, version_id, "a.txt")


def test_key_is_prefixed_and_cannot_be_hijacked_by_a_filename() -> None:
    workspace_id, document_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = originals_key(workspace_id, document_id, version_id, "../../../../etc/passwd")
    assert key == f"{ORIGINALS_PREFIX}/{workspace_id}/{document_id}/{version_id}"
    assert originals_key(workspace_id, document_id, version_id, "x.pdf").startswith(
        f"{ORIGINALS_PREFIX}/"
    )


def test_key_extension_is_normalized_and_bounded() -> None:
    workspace_id, document_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert originals_key(workspace_id, document_id, version_id, "REPORT.PDF").endswith(".pdf")
    # No suffix, or an implausibly long one, degrades to the bare version key.
    assert originals_key(workspace_id, document_id, version_id, "no-extension").endswith(
        str(version_id)
    )
    assert originals_key(workspace_id, document_id, version_id, "x." + "z" * 40).endswith(
        str(version_id)
    )


def test_bucket_and_checksum_helpers_are_the_single_definition() -> None:
    assert originals_bucket()
    assert sha256_hex(b"") == hashlib.sha256(b"").hexdigest()


# --- Postgres keeps metadata only (metric: byte columns in PG: 0) -----------


def test_postgres_model_has_no_byte_column() -> None:
    from ingestion.persistence import DocumentVersion

    columns = DocumentVersion.__table__.columns
    byte_columns = [c.name for c in columns if isinstance(c.type, sa.LargeBinary)]
    assert byte_columns == [], f"byte columns in PG: {byte_columns}"
    assert "storage_key" in columns
    assert "checksum" in columns
    assert columns["checksum"].nullable is False
    storage_key_type = columns["storage_key"].type
    assert isinstance(storage_key_type, sa.String) and storage_key_type.length == 512


# --- the pipeline maps seam failures onto registered job codes --------------


def _version(storage_key: str | None):
    from ingestion.persistence import DocumentVersion

    return DocumentVersion(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        version_number=1,
        checksum=sha256_hex(RAW_BYTES),
        size_bytes=len(RAW_BYTES),
        storage_key=storage_key,
    )


async def test_pipeline_loader_returns_bytes_when_the_object_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jobs.ingest_pipeline import load_original_bytes

    store = FakeStore()
    key = await _store(store, RAW_BYTES)

    async def _read(storage_key, *, checksum, **kwargs):
        assert checksum == sha256_hex(RAW_BYTES)
        return await store.get_object(originals_bucket(), storage_key)

    monkeypatch.setattr("jobs.ingest_pipeline.load_original", _read)
    assert await load_original_bytes(_version(key)) == RAW_BYTES


async def test_pipeline_loader_treats_a_missing_key_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key == no bytes == the EMPTY_DOCUMENT verdict the pipeline already
    gave ``content IS NULL``; this state's behaviour did not change."""
    from jobs.ingest_pipeline import load_original_bytes

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("must not touch the store without a storage_key")

    monkeypatch.setattr("jobs.ingest_pipeline.load_original", _must_not_be_called)
    assert await load_original_bytes(_version(None)) == b""


@pytest.mark.parametrize(
    ("failure", "code", "retryable"),
    [
        (OriginalChecksumMismatch("bytes do not match"), "INGESTION_ERROR", False),
        (OriginalMissing("object gone"), "INGESTION_ERROR", False),
        (StorageError("endpoint unreachable"), "DEPENDENCY_UNAVAILABLE", True),
    ],
)
async def test_pipeline_loader_maps_seam_failures_to_registered_codes(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    code: str,
    retryable: bool,
) -> None:
    from core.errors import canonical_code, normalize_code
    from jobs.ingest_pipeline import IngestPipelineError, load_original_bytes

    async def _raise(*args, **kwargs):
        raise failure

    monkeypatch.setattr("jobs.ingest_pipeline.load_original", _raise)
    with pytest.raises(IngestPipelineError) as excinfo:
        await load_original_bytes(_version("originals/w/d/v"))
    assert excinfo.value.code == code
    assert excinfo.value.retryable is retryable
    # Registered + canonical: the taxonomy contract scans pipeline literals.
    assert canonical_code(code) is not None
    assert normalize_code(code) == code
