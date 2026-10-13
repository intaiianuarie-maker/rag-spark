"""Integration: originals live in object storage, byte-identical end to end (T-V1-039).

Three proofs against the live compose stack (postgres + SeaweedFS), each of
which fails loudly rather than skipping silently:

1. **upload -> fetch**: the real upload endpoint stores the bytes through the
   storage seam, the row keeps ``storage_key`` + ``checksum`` and no bytes, and
   reading the object back reproduces the uploaded payload exactly (SHA-256
   equal to the row's checksum);
2. **the migration's backfill**: rows that predate this ticket — whose bytes
   used to sit in ``document_versions.content`` — round-trip through the object
   store just as exactly, and every row has a key;
3. **the pipeline's read**: ``load_original_bytes`` (what the worker calls
   before parsing) returns those same bytes from the live store.

Isolation: a unique ``INGEST_QUEUE_PREFIX`` for this module, so the compose
worker (still running the pre-T-V1-039 code until the coordinator redeploys)
never sees these jobs, and the keys are removed afterwards.

T-V1-078 verified this suite does **not** need the compose worker and does
**not** write live ``knowledge``. Measured over a full run of this file:

* **No compose-worker claim.** The uploaded job stays ``queued`` — ``ready``
  count is unchanged across the run, and ``knowledge`` held **2041 points
  before and after**. The compose worker runs in its own container with its own
  ``INGEST_QUEUE_PREFIX``, so it cannot consume these keys even after a
  redeploy.
* **No test here waits for a job to be processed.** All five proofs are
  upload-time or read-path assertions: the ``storage_key``/``checksum`` row, a
  direct ``load_original`` round-trip, ``load_original_bytes`` (what a worker
  would call) invoked directly, the backfill census, and schema shape. That is
  why no vector is ever produced.
* **Residue this suite does leave**, stated so it is not mistaken for
  pollution of the index: one ``document_versions`` row and one SeaweedFS
  object per run (not cleaned up), the module's redis queue keys (cleaned by
  the ``queue_prefix`` fixture), and one auth rate-limit key
  ``auth:rl:t039-<uuid>@example.test`` which is **not** a queue key and expires
  on its own (observed TTL 27s).

T-V1-078 also found this file is **red for a foreign reason**: any
``document_versions`` row created without a ``storage_key`` fails
``test_backfilled_rows_round_trip_through_the_object_store``. See the
T-V1-078 handoff row — the current offender is another suite's fixture.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from object_store_env import apply as apply_object_store_env

pytestmark = pytest.mark.integration

PASSWORD = "Correct-Horse-Battery-9"
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://knowledge:knowledge@localhost:5432/knowledge"
)

#: Binary payload that a text-only shortcut would corrupt (see the unit file).
PAYLOAD = b"T-V1-039 originals\r\n\x00\x01\x02 high bytes: \xe2\xe3\xcf\xd3 \xff\xfe\n"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _one(sql: str, params: dict | None = None) -> sa.Row | None:
    engine = sa.create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            return connection.execute(sa.text(sql), params or {}).first()
    finally:
        engine.dispose()


def _rows(sql: str, params: dict | None = None) -> list[sa.Row]:
    engine = sa.create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            return list(connection.execute(sa.text(sql), params or {}).all())
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def queue_prefix() -> Iterator[str]:
    prefix = f"t039-{uuid.uuid4().hex[:8]}:"
    os.environ["INGEST_QUEUE_PREFIX"] = prefix
    yield prefix
    try:
        import redis

        client = redis.Redis(host="127.0.0.1", port=6379)
        keys = list(client.scan_iter(match=f"ingest:{prefix}*"))
        if keys:
            client.delete(*keys)
    except Exception:  # pragma: no cover - cleanup is best-effort
        pass
    finally:
        os.environ.pop("INGEST_QUEUE_PREFIX", None)


@pytest.fixture(scope="module")
def client(queue_prefix: str):
    if not (_port_open(5432) and _port_open(6379)):
        pytest.skip("postgres/redis not reachable; run `make up`")
    apply_object_store_env()  # loud-skip when SeaweedFS is down

    from app.db import run_migrations

    run_migrations()  # idempotent: brings the schema to head (0009 + 0010)

    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as test_client:
        yield test_client


def _auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _new_user(client) -> str:
    email = f"t039-{uuid.uuid4().hex}@example.test"
    registered = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201, registered.text
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _create_workspace(client, token: str) -> str:
    response = client.post("/v1/workspaces", json={"name": "Originals"}, headers=_auth(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(client, token: str, workspace_id: str, filename: str, data: bytes):
    return client.post(
        f"/v1/workspaces/{workspace_id}/documents",
        files={"file": (filename, data, "text/plain")},
        headers=_auth(token),
    )


# --- 1. upload -> fetch, byte-identical -------------------------------------


def test_upload_stores_the_original_in_object_storage(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    filename = f"originals-{uuid.uuid4().hex}.txt"

    response = _upload(client, token, workspace, filename, PAYLOAD)
    assert response.status_code == 202, response.text
    version_id = response.json()["version_id"]

    row = _one(
        "SELECT storage_key, checksum, size_bytes FROM document_versions WHERE id = :id",
        {"id": version_id},
    )
    assert row is not None
    storage_key, checksum, size_bytes = row
    assert storage_key, "upload must leave a storage_key, not bytes"
    assert checksum == hashlib.sha256(PAYLOAD).hexdigest()
    assert size_bytes == len(PAYLOAD)

    fetched = asyncio.run(_load(storage_key, checksum))
    assert fetched == PAYLOAD, "upload -> fetch must be byte-identical"
    assert hashlib.sha256(fetched).hexdigest() == checksum


async def _load(storage_key: str, checksum: str) -> bytes:
    from storage.originals import load_original

    return await load_original(storage_key, checksum=checksum)


# --- the pipeline's own read path -------------------------------------------


def test_pipeline_reads_the_original_back_from_object_storage(client) -> None:
    from ingestion.persistence import DocumentVersion
    from jobs.ingest_pipeline import load_original_bytes

    row = _one(
        "SELECT id, document_id, checksum, size_bytes, storage_key "
        "FROM document_versions WHERE storage_key IS NOT NULL ORDER BY id DESC LIMIT 1"
    )
    assert row is not None, "no stored original found — did the backfill run?"
    version = DocumentVersion(
        id=row.id,
        document_id=row.document_id,
        version_number=1,
        checksum=row.checksum,
        size_bytes=row.size_bytes,
        storage_key=row.storage_key,
    )
    fetched = asyncio.run(load_original_bytes(version))
    assert len(fetched) == row.size_bytes
    assert hashlib.sha256(fetched).hexdigest() == row.checksum


# --- 2. the migration's backfill of pre-existing rows -----------------------


def test_backfilled_rows_round_trip_through_the_object_store() -> None:
    totals = _one(
        "SELECT count(*), count(storage_key) FROM document_versions"
    )
    assert totals is not None
    assert totals[0] > 0, "expected pre-existing rows"
    assert totals[0] == totals[1], (
        f"{totals[0] - totals[1]} row(s) have no storage_key — the backfill is incomplete"
    )

    sample = _rows(
        "SELECT id, storage_key, checksum, size_bytes FROM document_versions "
        "ORDER BY id DESC LIMIT 25"
    )
    from storage.originals import load_original

    async def _all() -> list[bytes]:
        return [
            await load_original(storage_key, checksum=checksum)
            for _id, storage_key, checksum, _size in sample
        ]

    for (_id, _key, checksum, size_bytes), fetched in zip(
        sample, asyncio.run(_all()), strict=True
    ):
        assert len(fetched) == size_bytes, "sized mismatch after the backfill"
        assert hashlib.sha256(fetched).hexdigest() == checksum, "SHA mismatch after backfill"


# --- 3. metric: byte columns in PG: 0 ---------------------------------------


def test_postgres_holds_no_byte_columns() -> None:
    byte_columns = _rows(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND data_type = 'bytea'"
    )
    assert byte_columns == [], f"byte columns in PG: {byte_columns}"


def test_document_versions_has_storage_key_and_checksum_only() -> None:
    columns = {
        row.name
        for row in _rows(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_name = 'document_versions'"
        )
    }
    assert "content" not in columns
    assert {"storage_key", "checksum", "size_bytes"} <= columns
