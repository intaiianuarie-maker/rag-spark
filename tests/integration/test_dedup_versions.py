"""Integration: checksum dedup, immutable versions, delete tombstone (M2-001d)."""

from __future__ import annotations

import asyncio
import os
import socket
import uuid

import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.integration

PASSWORD = "Correct-Horse-Battery-9"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def client():
    if not (_port_open(5432) and _port_open(6379)):
        pytest.skip("postgres/redis not reachable; run `make up`")

    from app.db import run_migrations

    run_migrations()

    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as test_client:
        yield test_client


def _auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _new_user(client) -> str:
    email = f"m2d-{uuid.uuid4().hex}@example.test"
    registered = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _create_workspace(client, token: str) -> str:
    response = client.post("/v1/workspaces", json={"name": "Dedup"}, headers=_auth(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(
    client,
    token: str,
    workspace_id: str,
    filename: str,
    data: bytes,
    content_type: str = "text/plain",
):
    return client.post(
        f"/v1/workspaces/{workspace_id}/documents",
        files={"file": (filename, data, content_type)},
        headers=_auth(token),
    )


def _run_job(job_id: str) -> None:
    from jobs.ingest_worker import process_one

    asyncio.run(process_one(uuid.UUID(job_id)))


def _db_scalar(query, params):
    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async def run():
        async with get_session_factory()() as session:
            return (await session.execute(text(query), params)).scalar_one()

    return asyncio.run(run())


def _chunk_count(version_id: str) -> int:
    return int(
        _db_scalar(
            "SELECT count(*) FROM chunks WHERE document_version_id = :v", {"v": version_id}
        )
    )


def _version_number(version_id: str) -> int:
    return int(
        _db_scalar(
            "SELECT version_number FROM document_versions WHERE id = :v", {"v": version_id}
        )
    )


def _is_superseded(version_id: str) -> bool:
    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async def run():
        async with get_session_factory()() as session:
            value = (
                await session.execute(
                    text("SELECT superseded_at FROM document_versions WHERE id = :v"),
                    {"v": version_id},
                )
            ).scalar_one()
            return value is not None

    return asyncio.run(run())


def _is_deleted(document_id: str) -> bool:
    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async def run():
        async with get_session_factory()() as session:
            value = (
                await session.execute(
                    text("SELECT deleted_at FROM documents WHERE id = :d"), {"d": document_id}
                )
            ).scalar_one()
            return value is not None

    return asyncio.run(run())


def _version_count(document_id: str) -> int:
    return int(
        _db_scalar(
            "SELECT count(*) FROM document_versions WHERE document_id = :d", {"d": document_id}
        )
    )


def test_identical_reupload_skips_parse_and_reuses_version(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    first = _upload(client, token, workspace, "doc.txt", b"identical content").json()
    _run_job(first["job_id"])
    version_id = first["version_id"]
    baseline = _chunk_count(version_id)
    assert baseline >= 1

    second = _upload(client, token, workspace, "doc.txt", b"identical content").json()
    assert second["status"] == "ready"  # short-circuited, never enqueued
    assert second["version_id"] == version_id
    assert _chunk_count(version_id) == baseline  # no re-parse / duplication
    assert _version_count(first["document_id"]) == 1


def test_changed_file_creates_immutable_version_and_supersedes(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    first = _upload(client, token, workspace, "doc.txt", b"version one content").json()
    _run_job(first["job_id"])
    old_version = first["version_id"]
    assert _chunk_count(old_version) >= 1

    second = _upload(client, token, workspace, "doc.txt", b"version two changed content").json()
    assert second["document_id"] == first["document_id"]
    assert second["version_id"] != old_version
    assert second["status"] == "queued"
    _run_job(second["job_id"])

    assert _version_number(second["version_id"]) == 2
    assert _chunk_count(second["version_id"]) >= 1
    assert _chunk_count(old_version) == 0  # superseded, not mutated
    assert _is_superseded(old_version) is True
    assert _version_count(first["document_id"]) == 2


def test_delete_tombstones_and_purges_postgres(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    uploaded = _upload(client, token, workspace, "del.txt", b"to be deleted").json()
    _run_job(uploaded["job_id"])
    document_id = uploaded["document_id"]
    assert _chunk_count(uploaded["version_id"]) >= 1

    response = client.delete(
        f"/v1/workspaces/{workspace}/documents/{document_id}", headers=_auth(token)
    )
    assert response.status_code == 204
    assert _chunk_count(uploaded["version_id"]) == 0
    assert _is_deleted(document_id) is True

    # A re-upload after deletion is a fresh document, not a dedup hit.
    again = _upload(client, token, workspace, "del.txt", b"to be deleted").json()
    assert again["document_id"] != document_id


def test_delete_requires_workspace_access(client) -> None:
    owner = _new_user(client)
    outsider = _new_user(client)
    workspace = _create_workspace(client, owner)
    uploaded = _upload(client, owner, workspace, "x.txt", b"secret").json()
    response = client.delete(
        f"/v1/workspaces/{workspace}/documents/{uploaded['document_id']}",
        headers=_auth(outsider),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "not_a_member"


def test_failed_then_reupload_reparses_instead_of_false_ready(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    corrupt = b"%PDF-1.4\nthis is not a real pdf"
    first = _upload(client, token, workspace, "broken.pdf", corrupt, "application/pdf").json()
    _run_job(first["job_id"])
    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async def job_state(job_id: str) -> str:
        async with get_session_factory()() as session:
            return (
                await session.execute(
                    text("SELECT status FROM ingestion_jobs WHERE id = :j"), {"j": job_id}
                )
            ).scalar_one()

    assert asyncio.run(job_state(first["job_id"])) == "failed"
    assert _chunk_count(first["version_id"]) == 0

    second = _upload(client, token, workspace, "broken.pdf", corrupt, "application/pdf").json()
    # Must NOT report a dishonest `ready`; the same version is re-enqueued.
    assert second["status"] == "queued", second
    assert second["version_id"] == first["version_id"]
    _run_job(second["job_id"])
    assert _chunk_count(first["version_id"]) == 0  # still corrupt -> still no chunks


def test_out_of_order_supersede_keeps_newer_version(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    first = _upload(client, token, workspace, "doc.txt", b"version one").json()
    second = _upload(client, token, workspace, "doc.txt", b"version two changed").json()
    old_version, new_version = first["version_id"], second["version_id"]

    # Process the NEWER job first, then the older one (retry-reorder scenario).
    _run_job(second["job_id"])
    newer_chunks = _chunk_count(new_version)
    assert newer_chunks >= 1
    assert _is_superseded(old_version) is True

    _run_job(first["job_id"])

    # The older completion must not clobber the newer version.
    assert _chunk_count(new_version) == newer_chunks
    assert _is_superseded(new_version) is False


def test_cross_workspace_dedup_is_isolated(client) -> None:
    token = _new_user(client)
    workspace_a = _create_workspace(client, token)
    workspace_b = _create_workspace(client, token)
    payload = b"identical bytes for the A/B dedup test"

    a = _upload(client, token, workspace_a, "same.txt", payload).json()
    b = _upload(client, token, workspace_b, "same.txt", payload).json()

    assert a["document_id"] != b["document_id"]
    assert a["version_id"] != b["version_id"]
    _run_job(a["job_id"])
    _run_job(b["job_id"])
    assert _chunk_count(a["version_id"]) >= 1
    assert _chunk_count(b["version_id"]) >= 1

