"""Integration: knowledge read endpoints (TASK-M2-003).

Four frozen reads — collections list, collection documents, document detail,
document jobs. Non-member must get 403 on all four. Jobs are driven directly
(``process_one``) so results do not depend on the live worker container.

Env: postgres + redis on localhost (same gating as the M2-001c job tests).
"""

from __future__ import annotations

import asyncio
import hashlib
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
    email = f"m2003-{uuid.uuid4().hex}@example.test"
    registered = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201, registered.text
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _create_workspace(client, token: str, name: str = "Knowledge") -> str:
    response = client.post("/v1/workspaces", json={"name": name}, headers=_auth(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_job(
    workspace_id: str,
    *,
    filename: str = "note.txt",
    data: bytes = b"Hello world.\n\nSecond paragraph.",
    mime_type: str = "text/plain",
    title: str | None = None,
) -> tuple[str, str, str]:
    """Create Document/Version/Job rows directly (no queue), return ids."""
    from ingestion.persistence import (
        Document,
        DocumentVersion,
        IngestionJob,
        JobStage,
        JobStatus,
        get_session_factory,
    )

    async def run() -> tuple[str, str, str]:
        async with get_session_factory()() as session:
            document = Document(
                workspace_id=uuid.UUID(workspace_id),
                title=title or filename,
                source_filename=filename,
                mime_type=mime_type,
            )
            session.add(document)
            await session.flush()
            version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                checksum=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                mime_type=mime_type,
                content=data,
            )
            session.add(version)
            await session.flush()
            document.current_version_id = version.id
            job = IngestionJob(
                workspace_id=document.workspace_id,
                document_id=document.id,
                document_version_id=version.id,
                status=JobStatus.QUEUED.value,
                stage=JobStage.QUEUED.value,
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)
            return str(document.id), str(version.id), str(job.id)

    return asyncio.run(run())


def _run_job(job_id: str) -> None:
    from jobs.ingest_worker import process_one

    asyncio.run(process_one(uuid.UUID(job_id)))


# --- 1. collections list ----------------------------------------------------


def test_collections_list_is_the_workspace_with_consistent_count(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token, name="Reads A")
    _create_job(workspace, filename="a.txt")
    _create_job(workspace, filename="b.txt")

    response = client.get(
        f"/v1/workspaces/{workspace}/collections", headers=_auth(token)
    )
    assert response.status_code == 200, response.text
    collections = response.json()
    assert len(collections) == 1
    collection = collections[0]
    assert set(collection) == {"id", "name", "document_count", "created_at"}
    assert collection["id"] == workspace
    assert collection["name"] == "Reads A"
    assert collection["created_at"]

    documents = client.get(
        f"/v1/workspaces/{workspace}/collections/{workspace}/documents",
        headers=_auth(token),
    ).json()
    # frozen acceptance: document_count == len(documents)
    assert collection["document_count"] == len(documents) == 2


def test_collection_id_must_be_the_workspace(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    response = client.get(
        f"/v1/workspaces/{workspace}/collections/{uuid.uuid4()}/documents",
        headers=_auth(token),
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "collection_not_found"


# --- 2. collection documents ------------------------------------------------


def test_collection_documents_shape_and_counts(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    document_id, version_id, job_id = _create_job(workspace, filename="ready.txt")
    _run_job(job_id)

    response = client.get(
        f"/v1/workspaces/{workspace}/collections/{workspace}/documents",
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    documents = response.json()
    assert len(documents) == 1
    document = documents[0]
    # exact frozen shape
    assert set(document) == {
        "id",
        "title",
        "filename",
        "status",
        "version_count",
        "chunk_count",
        "updated_at",
    }
    assert document["id"] == document_id
    assert document["filename"] == "ready.txt"
    assert document["status"] == "ready"  # worker stage, not raw status
    assert document["version_count"] == 1
    assert document["chunk_count"] > 0
    assert document["updated_at"]

    # chunk_count must equal the DB count for the current version
    from ingestion.persistence import ChunkRow, get_session_factory
    from sqlalchemy import func, select

    async def run() -> int:
        async with get_session_factory()() as session:
            result = await session.execute(
                select(func.count())
                .select_from(ChunkRow)
                .where(ChunkRow.document_version_id == uuid.UUID(version_id))
            )
            return int(result.scalar_one())

    assert document["chunk_count"] == asyncio.run(run())


def test_deleted_document_is_excluded_and_detail_404(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    document_id, _version_id, job_id = _create_job(workspace, filename="gone.txt")
    _run_job(job_id)

    deleted = client.delete(
        f"/v1/workspaces/{workspace}/documents/{document_id}", headers=_auth(token)
    )
    assert deleted.status_code == 204

    listing = client.get(
        f"/v1/workspaces/{workspace}/collections/{workspace}/documents",
        headers=_auth(token),
    ).json()
    assert listing == []

    detail = client.get(
        f"/v1/workspaces/{workspace}/documents/{document_id}", headers=_auth(token)
    )
    assert detail.status_code == 404
    assert detail.json()["detail"]["error"] == "document_not_found"


# --- 3. document detail -----------------------------------------------------


def test_document_detail_carries_versions_and_jobs(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    document_id, version_id, job_id = _create_job(
        workspace, filename="detail.txt", data=b"Alpha beta. Gamma delta."
    )
    _run_job(job_id)

    response = client.get(
        f"/v1/workspaces/{workspace}/documents/{document_id}", headers=_auth(token)
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert {
        "id",
        "title",
        "filename",
        "status",
        "version_count",
        "chunk_count",
        "updated_at",
        "created_at",
        "size_bytes",
        "mime_type",
        "current_version",
        "versions",
        "jobs",
    } <= set(detail)
    assert detail["id"] == document_id
    assert detail["status"] == "ready"
    assert detail["current_version"]["id"] == version_id
    assert detail["current_version"]["version"] == 1
    assert len(detail["versions"]) == 1
    assert detail["versions"][0]["version"] == 1
    assert detail["size_bytes"] == len(b"Alpha beta. Gamma delta.")
    assert detail["mime_type"] == "text/plain"
    assert len(detail["jobs"]) == 1
    job = detail["jobs"][0]
    assert set(job) == {"id", "state", "attempts", "error", "created_at", "updated_at"}
    assert job["id"] == job_id
    assert job["state"] == "ready"
    assert job["attempts"] >= 1
    assert job["error"] is None


def test_document_detail_404_for_other_workspace(client) -> None:
    owner = _new_user(client)
    workspace_a = _create_workspace(client, owner, name="A")
    workspace_b = _create_workspace(client, owner, name="B")
    document_id, _v, _j = _create_job(workspace_a, filename="secret.txt")

    # Owner of both workspaces: the document must not leak across workspace ids.
    response = client.get(
        f"/v1/workspaces/{workspace_b}/documents/{document_id}", headers=_auth(owner)
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "document_not_found"


# --- 4. document jobs -------------------------------------------------------


def test_document_jobs_reflect_failed_worker_state(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    # unsupported mime -> pipeline fails non-retryably
    document_id, _version_id, job_id = _create_job(
        workspace, filename="audio.mp3", mime_type="audio/mpeg", data=b"not audio"
    )
    _run_job(job_id)

    response = client.get(
        f"/v1/workspaces/{workspace}/documents/{document_id}/jobs",
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    jobs = response.json()
    assert len(jobs) == 1
    assert set(jobs[0]) == {"id", "state", "attempts", "error", "created_at", "updated_at"}
    assert jobs[0]["id"] == job_id
    assert jobs[0]["state"] == "failed"
    assert jobs[0]["error"]  # error_message/code surfaced


def test_document_jobs_404_for_unknown_document(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    response = client.get(
        f"/v1/workspaces/{workspace}/documents/{uuid.uuid4()}/jobs",
        headers=_auth(token),
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "document_not_found"


# --- 5. non-member 403 on all four ------------------------------------------


def test_non_member_403_on_all_four_reads(client) -> None:
    owner = _new_user(client)
    outsider = _new_user(client)
    workspace = _create_workspace(client, owner)
    document_id, _v, _j = _create_job(workspace, filename="private.txt")

    paths = [
        f"/v1/workspaces/{workspace}/collections",
        f"/v1/workspaces/{workspace}/collections/{workspace}/documents",
        f"/v1/workspaces/{workspace}/documents/{document_id}",
        f"/v1/workspaces/{workspace}/documents/{document_id}/jobs",
    ]
    for path in paths:
        response = client.get(path, headers=_auth(outsider))
        assert response.status_code == 403, (path, response.status_code, response.text)
        assert response.json()["detail"]["error"] == "not_a_member", path


# --- 6. relocated job poll (decision 1A) ------------------------------------


def test_job_poll_moved_to_jobs_route(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    _document_id, _version_id, job_id = _create_job(workspace, filename="poll.txt")
    _run_job(job_id)

    moved = client.get(
        f"/v1/workspaces/{workspace}/jobs/{job_id}", headers=_auth(token)
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["job_id"] == job_id
    assert moved.json()["status"] == "ready"

    # The old path now resolves to the document-detail read -> 404 for a job id.
    old = client.get(
        f"/v1/workspaces/{workspace}/documents/{job_id}", headers=_auth(token)
    )
    assert old.status_code == 404
    assert old.json()["detail"]["error"] == "document_not_found"
