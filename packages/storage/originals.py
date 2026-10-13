"""Document originals behind the storage seam (T-V1-039, PD-005).

Postgres keeps ``document_versions.storage_key`` + ``checksum`` and nothing
else — the original bytes live in the S3/SeaweedFS object store and are only
ever reached through :class:`storage.interfaces.StorageAdapter`. This module is
the single place that turns a document version into an object key and back, so
the upload path, the ingestion pipeline and the migration backfill cannot drift
apart on the key scheme or on what "the bytes are intact" means.

Integrity rule (ticket metric *round-trip SHA match: yes*):

- :func:`put_original` refuses to write bytes whose SHA-256 is not the
  caller's declared checksum, then (``verify=True``) reads the object back and
  refuses again if what landed no longer hashes to it;
- :func:`load_original` refuses to hand back bytes whose SHA-256 is not the row's
  ``checksum`` — a corrupt or swapped object can never reach a parser.

Every message raised from here is written for a job row: no hostnames, no
endpoint URLs, no vendor exception text (AGENTS safe-user-reason). The vendor
detail goes to the log, never to ``ingestion_jobs.error_message``.

Only the adapter touches boto3 (PD-005 vendor guard): this module imports
``storage.adapters`` lazily and never a vendor SDK directly.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid

from core.config import get_settings

from .interfaces import StorageAdapter, StorageError

#: Key namespace for document originals (prefix-GC by workspace/document).
ORIGINALS_PREFIX = "originals"

#: Object-key suffixes we will append from an upload filename: lowercase
#: alphanumerics only, so a hostile filename cannot inject "/" or "..".
_EXTENSION_RE = re.compile(r"\.[a-z0-9]{1,12}$")

#: ``Error.Code`` values that mean "this object does not exist" rather than
#: "the store could not be reached" — the difference between a terminal
#: failure and a retryable one.
_ABSENT_CODES = frozenset({"NoSuchKey", "NotFound", "404"})


class OriginalMissing(StorageError):
    """No object for this version: either no ``storage_key`` or no object."""


class OriginalChecksumMismatch(StorageError):
    """Bytes do not hash to the checksum Postgres recorded for them."""


def sha256_hex(data: bytes) -> str:
    """The one checksum definition used on both sides of the seam."""
    return hashlib.sha256(data).hexdigest()


def originals_bucket() -> str:
    """Bucket holding the originals — same source compose gives the services."""
    return (
        os.getenv("S3_BUCKET")
        or os.getenv("MINIO_BUCKET")
        or get_settings().minio_bucket
    )


def _endpoint() -> str:
    """S3 endpoint, never empty.

    An empty endpoint makes boto3 fall through to AWS — never what "store our
    document originals" means (PD-005 binds object storage to SeaweedFS/MinIO
    behind this seam). Services get the value from compose; host-side tooling
    (alembic backfill, tests) gets it from these env vars or from settings,
    whose default is the loopback- published SeaweedFS port.
    """
    raw = (
        os.getenv("S3_ENDPOINT_URL")
        or os.getenv("SEAWEEDFS_ENDPOINT")
        or get_settings().minio_endpoint
        or ""
    )
    return raw.rstrip("/")


def _credentials() -> tuple[str, str]:
    settings = get_settings()
    access = (
        os.getenv("S3_ACCESS_KEY")
        or os.getenv("SEAWEEDFS_ACCESS_KEY")
        or settings.minio_access_key
    )
    secret = (
        os.getenv("S3_SECRET_KEY")
        or os.getenv("SEAWEEDFS_SECRET_KEY")
        or settings.minio_secret_key
    )
    return access, secret


#: Process-wide adapter: building a boto3 client per object would pay the
#: credential/endpoint handshake on every upload. Env is fixed at process
#: start, so one client is safe; tests inject their own instead of swapping
#: this (see the ``adapter=`` argument on the two operations below).
_adapter: StorageAdapter | None = None


def get_adapter() -> StorageAdapter:
    """The adapter this process stores originals through (lazy import)."""
    global _adapter
    if _adapter is None:
        # Imported here, not at module scope: only the adapter may pull boto3
        # in, and a service that never touches an original never pays for it.
        from .adapters.seaweedfs_s3 import SeaweedFSAdapter

        access, secret = _credentials()
        _adapter = SeaweedFSAdapter(endpoint_url=_endpoint(), access_key=access, secret_key=secret)
    return _adapter


def _extension(filename: str | None) -> str:
    """Sanitized suffix for a stored original ("" when there is nothing safe)."""
    if not filename:
        return ""
    suffix = os.path.splitext(filename)[1].lower()
    if not _EXTENSION_RE.fullmatch(suffix):
        return ""
    return suffix


def originals_key(
    workspace_id: uuid.UUID | str,
    document_id: uuid.UUID | str,
    version_id: uuid.UUID | str,
    filename: str | None = None,
) -> str:
    """Deterministic object key for one document version's original bytes.

    Scoped by workspace and document so a prefix listing is exactly one
    document's originals (deletion/GC can be scoped later without re-keying),
    and by version id so two versions of the same document never share an
    object — replacing a document cannot corrupt the version it replaced.
    """
    return (
        f"{ORIGINALS_PREFIX}/{workspace_id}/{document_id}/{version_id}"
        f"{_extension(filename)}"
    )


def _object_absent(exc: StorageError) -> bool:
    """True when the adapter failed with "no such object", not a transport error."""
    cause = exc.__cause__
    if cause is None:
        return False
    response = getattr(cause, "response", None)
    if not isinstance(response, dict):  # EndpointConnectionError & friends
        return False
    error = response.get("Error")
    if not isinstance(error, dict):
        return False
    return str(error.get("Code", "")) in _ABSENT_CODES


async def put_original(
    *,
    workspace_id: uuid.UUID | str,
    document_id: uuid.UUID | str,
    version_id: uuid.UUID | str,
    data: bytes,
    checksum: str,
    filename: str | None = None,
    bucket: str | None = None,
    adapter: StorageAdapter | None = None,
    verify: bool = True,
) -> str:
    """Store ``data`` and return the ``storage_key`` Postgres will keep.

    Called *before* the row commits: an object without a row is inert garbage,
    a row without an object is a document that can never be parsed again.

    ``checksum`` is the caller's declared SHA-256 (the value it is about to
    write into ``document_versions.checksum``). Two refusals, both terminal:
    bytes that do not hash to it are never written, and — when ``verify`` —
    bytes that do not *still* hash to it after the write are reported instead
    of being silently kept (read-after-write proof, ticket metric).
    """
    actual = sha256_hex(data)
    if actual != checksum:
        raise OriginalChecksumMismatch(
            f"refusing to store {len(data)} bytes: SHA-256 {actual} != declared {checksum}"
        )
    store = adapter if adapter is not None else get_adapter()
    target = bucket if bucket is not None else originals_bucket()
    key = originals_key(workspace_id, document_id, version_id, filename)
    await store.put_object(
        target,
        key,
        data,
        metadata={"sha256": checksum, "size_bytes": str(len(data))},
    )
    if verify:
        landed = await store.get_object(target, key)
        if sha256_hex(landed) != checksum:
            raise OriginalChecksumMismatch(f"read-after-write verification failed for {key}")
    return key


async def load_original(
    storage_key: str | None,
    *,
    checksum: str,
    bucket: str | None = None,
    adapter: StorageAdapter | None = None,
) -> bytes:
    """Fetch a stored original, refusing anything that fails verification.

    Raises :class:`OriginalMissing` when there is nothing to fetch (no key, or
    the object is gone) and :class:`OriginalChecksumMismatch` when what is
    there is not what was stored — both terminal for a reader: retrying cannot
    conjure back bytes that were never written or that rotted. Any other
    :class:`StorageError` (endpoint down, timeout) is the caller's to retry.
    """
    if not storage_key:
        raise OriginalMissing("document version has no storage_key")
    store = adapter if adapter is not None else get_adapter()
    target = bucket if bucket is not None else originals_bucket()
    try:
        data = await store.get_object(target, storage_key)
    except StorageError as exc:
        if _object_absent(exc):
            raise OriginalMissing("original object is not in the store") from exc
        # Transport/dependency failure: the caller retries, and the vendor
        # message (endpoint, host) stays out of the job row.
        raise
    actual = sha256_hex(data)
    if actual != checksum:
        raise OriginalChecksumMismatch(
            f"original failed SHA-256 verification: stored {actual}, row says {checksum}"
        )
    return data
