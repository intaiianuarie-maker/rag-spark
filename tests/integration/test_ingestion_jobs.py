"""Integration: upload endpoint + worker pipeline (M2-001c).

Pipeline failure tests create their rows **directly** (not via the API queue) so
they are deterministic even while a live worker container consumes the shared
Redis queue; the upload-endpoint test only asserts the synchronous 202 contract.
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
    email = f"m2c-{uuid.uuid4().hex}@example.test"
    registered = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _create_workspace(client, token: str) -> str:
    response = client.post("/v1/workspaces", json={"name": "Ingest"}, headers=_auth(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(
    client,
    token: str,
    workspace_id: str,
    filename: str,
    data: bytes,
    content_type: str,
    charset: str | None = None,
):
    form = {"charset": charset} if charset else None
    return client.post(
        f"/v1/workspaces/{workspace_id}/documents",
        files={"file": (filename, data, content_type)},
        data=form,
        headers=_auth(token),
    )


def _job_status(client, token: str, workspace_id: str, job_id: str) -> dict:
    response = client.get(
        f"/v1/workspaces/{workspace_id}/jobs/{job_id}", headers=_auth(token)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_job(workspace_id: str, data: bytes, mime_type: str, charset=None) -> tuple[str, str]:
    """Create Document/Version/Job rows directly (no queue) for pipeline tests."""
    from ingestion.persistence import (
        Document,
        DocumentVersion,
        IngestionJob,
        JobStage,
        JobStatus,
        get_session_factory,
    )

    async def run() -> tuple[str, str]:
        async with get_session_factory()() as session:
            document = Document(
                workspace_id=uuid.UUID(workspace_id),
                title="direct",
                source_filename="direct.bin",
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
                charset=charset,
                content=data,
            )
            session.add(version)
            await session.flush()
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
            return str(job.id), str(version.id)

    return asyncio.run(run())


def _run_job(job_id: str) -> None:
    from jobs.ingest_worker import process_one

    asyncio.run(process_one(uuid.UUID(job_id)))


def _chunk_count(version_id: str) -> int:
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

    return asyncio.run(run())


def test_upload_endpoint_returns_202_queued(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    response = _upload(
        client, token, workspace, "note.txt", b"Hello world.\n\nSecond paragraph.", "text/plain"
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["job_id"] and body["document_id"] and body["version_id"]
    # The job is observable immediately (the live worker may already be running it).
    status = _job_status(client, token, workspace, body["job_id"])
    assert status["status"] in {"queued", "running", "ready", "failed"}


def test_pipeline_reaches_ready_with_chunks(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    job_id, version_id = _create_job(workspace, b"Hello world.\n\nSecond paragraph.", "text/plain")

    _run_job(job_id)

    status = _job_status(client, token, workspace, job_id)
    assert status["status"] == "ready"
    assert status["stage"] == "ready"
    assert status["attempts"] == 1
    assert _chunk_count(version_id) >= 1


def test_charset_hint_round_trip(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    job_id, _version = _create_job(
        workspace, "Olá ção".encode("cp1252"), "text/plain", charset="cp1252"
    )
    _run_job(job_id)
    assert _job_status(client, token, workspace, job_id)["status"] == "ready"


def test_unsupported_mime_fails_without_retry(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    job_id, _version = _create_job(workspace, b"not-really-audio", "audio/mpeg")

    _run_job(job_id)

    status = _job_status(client, token, workspace, job_id)
    assert status["status"] == "failed"
    # T-V1-049: the ingestion wire moved to the canonical UPPER_SNAKE_CASE code.
    assert status["error_code"] == "UNSUPPORTED_MIME_TYPE"
    assert status["attempts"] == 1


def test_poison_message_is_capped_after_retries(client, monkeypatch: pytest.MonkeyPatch) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    job_id, _version = _create_job(workspace, b"content", "text/plain")

    class Boom:
        async def parse(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr("jobs.ingest_pipeline.parser_for_mime", lambda mime: Boom())
    for _ in range(3):
        _run_job(job_id)

    status = _job_status(client, token, workspace, job_id)
    assert status["status"] == "failed"
    assert status["attempts"] == 3
    assert status["error_code"] == "PARSE_FAILED"


def test_upload_requires_authentication(client) -> None:
    response = client.post(
        f"/v1/workspaces/{uuid.uuid4()}/documents",
        files={"file": ("x.txt", b"x", "text/plain")},
    )
    assert response.status_code == 401


def test_upload_rejects_oversize_while_streaming(client, monkeypatch: pytest.MonkeyPatch) -> None:
    # Lower the cap so the streaming fence is exercised without a 25 MiB body.
    monkeypatch.setattr("app.documents.upload.MAX_DOCUMENT_BYTES", 1024)
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    response = _upload(client, token, workspace, "big.txt", b"x" * 4096, "text/plain")
    assert response.status_code == 413
    # Canonical uppercase literal, emitted inline by apps/api/app/documents/upload.py
    # (T-V1-053 uppercased the upload.py residue; it now matches the worker/job path
    # and the registry wire — see the wire policy in packages/core/errors.py).
    assert response.json()["detail"]["error"] == "DOCUMENT_TOO_LARGE"


def test_upload_rejects_unknown_charset(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    response = _upload(
        client, token, workspace, "x.txt", b"hello", "text/plain", charset="not-a-codec"
    )
    assert response.status_code == 400
    # Canonical uppercase literal from upload.py (T-V1-053); INVALID_CHARSET has no
    # worker-side emitter, so this envelope assertion is where its registry wire is
    # exercised end to end.
    assert response.json()["detail"]["error"] == "INVALID_CHARSET"
