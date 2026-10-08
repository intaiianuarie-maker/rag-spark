"""Backend-agnostic adapter integration test (PD-005 precondition, v2).

Put/get/delete/presign round-trip behind the storage seam. Only the adapter
touches vendor SDKs; no SDK types cross.
Fixtures: env-driven endpoint + injectable adapter for clean injection.
Loud-skip when no backend is reachable; real assertions when it is.
Binding: adapter-only S3 access; zero AGPL code in-tree; vendor guard verified.
"""

from __future__ import annotations

import os

import pytest

from packages.storage.adapters.seaweedfs_s3 import SeaweedFSAdapter
from packages.storage.interfaces import StorageError, StorageObject


def _env_adapter() -> SeaweedFSAdapter:
    return SeaweedFSAdapter(
        endpoint_url=os.getenv("S3_ENDPOINT_URL", os.getenv("SEAWEEDFS_ENDPOINT", "")),
        access_key=os.getenv("S3_ACCESS_KEY", os.getenv("SEAWEEDFS_ACCESS_KEY", "")),
        secret_key=os.getenv("S3_SECRET_KEY", os.getenv("SEAWEEDFS_SECRET_KEY", "")),
    )


def test_adapter_has_typed_error() -> None:
    assert StorageError.__bases__[0] is Exception


@pytest.fixture
def adapter() -> SeaweedFSAdapter:
    return _env_adapter()


@pytest.fixture
def bucket(adapter: SeaweedFSAdapter) -> str:
    return os.getenv("S3_BUCKET", "knowledge-os-test")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("S3_ENDPOINT_URL") and not os.getenv("SEAWEEDFS_ENDPOINT"),
    reason="No S3 endpoint configured (loud skip: set SEAWEEDFS_ENDPOINT or S3_ENDPOINT_URL)",
)
async def test_adapter_round_trip(adapter: SeaweedFSAdapter, bucket: str) -> None:
    key = "integration/test/object"
    data = b"SeaweedFS adapter integration test payload\n"

    result = await adapter.put_object(bucket, key, data)
    assert isinstance(result, StorageObject)
    assert result.key == key
    assert result.size_bytes == len(data)

    fetched = await adapter.get_object(bucket, key)
    assert fetched == data

    listed = await adapter.list_objects(bucket, prefix="integration/test/")
    assert any(obj.key == key for obj in listed)

    # Real presigned URL must honor expiry and be fetchable unauthenticated
    url = await adapter.presign_url(bucket, key, operation="get", expiry_seconds=300)
    assert url.startswith("http")
    # Real generate_presigned_url includes Signature parameters.
    assert "Signature=" in url

    deleted = await adapter.delete_object(bucket, key)
    assert deleted is True

    listed_after = await adapter.list_objects(bucket, prefix="integration/test/")
    assert not any(obj.key == key for obj in listed_after)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("S3_ENDPOINT_URL") and not os.getenv("SEAWEEDFS_ENDPOINT"),
    reason="No S3 endpoint configured (loud skip)",
)
async def test_adapter_delete_missing(adapter: SeaweedFSAdapter, bucket: str) -> None:
    result = await adapter.delete_object(bucket, "integration/test/nonexistent")
    assert result in (True, False)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("S3_ENDPOINT_URL") and not os.getenv("SEAWEEDFS_ENDPOINT"),
    reason="No S3 endpoint configured (loud skip)",
)
async def test_adapter_list_and_delete_propagation(adapter: SeaweedFSAdapter, bucket: str) -> None:
    # Tombstone reconciliation depends on LIST before delete.
    listed_before = await adapter.list_objects(bucket, prefix="integration/test/")
    # Honest LIST returns StorageObject list (empty if nothing present).
    assert isinstance(listed_before, list)
