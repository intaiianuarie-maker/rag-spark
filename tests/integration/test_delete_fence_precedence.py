"""T-V1-080: the delete fence outranks a missing original, and rot stays rot.

T-055/T-072 purge a tombstoned document's original, so a job that redelivers
after a delete reached the originals load with no object behind it. The load
raised ``OriginalMissing`` -> ``INGESTION_ERROR`` *before* the pre-write fence
could disclose ``DOCUMENT_DELETED``: a document an operator deleted on purpose
was reported as unreadable evidence. Safety was never at stake (the job still
failed and drained, nothing was resurrected) — only the disclosed code was
wrong, and the acceptance record explicitly did not waive it.

Both directions are pinned here, because the fix is worthless if it collapses
the distinction into "everything is a delete":

* deleted document + purged original -> ``DOCUMENT_DELETED`` (the fence wins);
* **live** document + missing original -> ``INGESTION_ERROR`` (rot is rot).

The live-path runs use the T-071 scratch-collection seam, so no vector point is
ever written into the live ``knowledge`` collection.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

# T-V1-071: the REAL ingest pipeline, never into the live `knowledge` collection.
# Export before any app import — pydantic settings cache on first read.
from vector_collection_env import assert_not_live, use_scratch_collection

VECTOR_COLLECTION = use_scratch_collection("delfence")
# ...and keep the compose worker off this suite's jobs, so the only process that
# runs them is this one (which points at the scratch name).
os.environ.setdefault("INGEST_QUEUE_PREFIX", "t071-delfence:")
assert_not_live(VECTOR_COLLECTION)

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.integration

PASSWORD = "Correct-Horse-Battery-9"


# --- harness ----------------------------------------------------------------


@pytest.fixture
def client():
    import socket

    def _port_open(port: int) -> bool:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            return probe.connect_ex(("127.0.0.1", port)) == 0

    if not (_port_open(5432) and _port_open(6379)):
        pytest.skip("postgres/redis not reachable; run `make up`")

    from app.db import run_migrations

    run_migrations()

    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as test_client:
        yield test_client


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _new_user(client) -> str:
    email = f"t080-{uuid.uuid4().hex}@example.test"
    registered = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201, registered.text
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _create_workspace(client, token: str) -> str:
    response = client.post(
        "/v1/workspaces", json={"name": f"t080 {uuid.uuid4().hex[:8]}"}, headers=_auth(token)
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def _upload(client, token: str, workspace_id: str, name: str, data: bytes) -> dict:
    """The real upload path — this is what stores the original object."""
    response = client.post(
        f"/v1/workspaces/{workspace_id}/documents",
        files={"file": (name, data, "text/plain")},
        headers=_auth(token),
    )
    assert response.status_code in (200, 201, 202), response.text
    return response.json()


def _delete_document(client, token: str, workspace_id: str, document_id: str) -> None:
    response = client.delete(
        f"/v1/workspaces/{workspace_id}/documents/{document_id}", headers=_auth(token)
    )
    assert response.status_code in (204, 207), response.text


def _run_job(job_id: str) -> None:
    from jobs.ingest_worker import process_one

    asyncio.run(process_one(uuid.UUID(job_id)))


def _job_row(job_id: str) -> dict:
    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async def run() -> dict:
        async with get_session_factory()() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, error_code, error_message FROM ingestion_jobs "
                        "WHERE id = CAST(:j AS uuid)"
                    ),
                    {"j": job_id},
                )
            ).first()
            assert row is not None, "job row missing"
            return {"status": row[0], "error_code": row[1], "error_message": row[2]}

    return asyncio.run(run())


def _version_storage_key(version_id: str) -> str | None:
    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async def run() -> str | None:
        async with get_session_factory()() as session:
            return (
                await session.execute(
                    text("SELECT storage_key FROM document_versions WHERE id = CAST(:v AS uuid)"),
                    {"v": version_id},
                )
            ).scalar_one_or_none()

    return asyncio.run(run())


def _deleted_at(document_id: str):
    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async def run():
        async with get_session_factory()() as session:
            return (
                await session.execute(
                    text("SELECT deleted_at FROM documents WHERE id = CAST(:d AS uuid)"),
                    {"d": document_id},
                )
            ).scalar_one()

    return asyncio.run(run())


def _drop_original(storage_key: str) -> None:
    """Simulate storage rot: the row still points at an object that is gone."""
    from object_store_env import apply as apply_object_store_env
    from storage.originals import get_adapter, originals_bucket

    apply_object_store_env()

    async def run() -> None:
        adapter = get_adapter()
        await adapter.delete_object(originals_bucket(), storage_key)

    asyncio.run(run())


def _original_state(storage_key: str | None) -> str:
    """``present`` / ``missing`` — the precondition the fence ordering turns on."""
    from object_store_env import apply as apply_object_store_env
    from storage.originals import OriginalMissing, load_original

    apply_object_store_env()

    async def run() -> str:
        try:
            await load_original(storage_key, checksum="0" * 64)
        except OriginalMissing:
            return "missing"
        except Exception:  # a checksum complaint still proves the object exists
            return "present"
        return "present"

    return asyncio.run(run())


# --- direction 1: the delete fence wins ---------------------------------------


def test_tombstoned_document_discloses_document_deleted_not_storage_rot(client) -> None:
    """T-V1-080 metric: delete wins over a purged original.

    The precondition is asserted, not assumed: the delete really did purge the
    object (T-055/T-072), so this is exactly the situation that used to disclose
    ``INGESTION_ERROR``. Revert the fence-first ordering and this test fails.
    """
    token = _new_user(client)
    workspace_id = _create_workspace(client, token)
    uploaded = _upload(
        client,
        token,
        workspace_id,
        "t080-deleted.txt",
        f"tombstoned evidence {uuid.uuid4().hex}".encode(),
    )
    version_id = uploaded["version_id"]
    storage_key = _version_storage_key(version_id)
    assert storage_key, "the real upload path must leave a storage_key"

    _delete_document(client, token, workspace_id, uploaded["document_id"])
    assert _deleted_at(uploaded["document_id"]) is not None, "the tombstone must be set"
    assert _original_state(storage_key) == "missing", (
        "the delete path must have purged the original — without this the test "
        "would pass without exercising the fence/load race at all"
    )

    _run_job(uploaded["job_id"])

    row = _job_row(uploaded["job_id"])
    assert row["status"] == "failed", row
    assert row["error_code"] == "DOCUMENT_DELETED", row
    assert row["error_code"] != "INGESTION_ERROR", (
        f"a deleted document reported as storage rot: {row}"
    )


# --- direction 2: rot on a LIVE document stays rot ---------------------------


def test_missing_original_on_a_live_document_still_discloses_ingestion_error(client) -> None:
    """The distinction must survive: no tombstone, missing object ⇒ rot.

    This is the test that fails the other way — if the fix reported
    ``DOCUMENT_DELETED`` for every missing original, or collapsed the two
    codes, this fails while direction 1 still passes.
    """
    token = _new_user(client)
    workspace_id = _create_workspace(client, token)
    uploaded = _upload(
        client,
        token,
        workspace_id,
        "t080-rot.txt",
        f"live evidence whose object will rot {uuid.uuid4().hex}".encode(),
    )
    storage_key = _version_storage_key(uploaded["version_id"])
    assert storage_key

    _drop_original(storage_key)
    assert _deleted_at(uploaded["document_id"]) is None, (
        "the document is live — this is storage rot, not a delete"
    )
    assert _original_state(storage_key) == "missing", "the original must really be gone"

    _run_job(uploaded["job_id"])

    row = _job_row(uploaded["job_id"])
    assert row["status"] == "failed", row
    assert row["error_code"] == "INGESTION_ERROR", (
        f"storage rot on a live document must stay INGESTION_ERROR: {row}"
    )
    assert row["error_code"] != "DOCUMENT_DELETED", (
        f"a live document was reported as deleted: {row}"
    )
