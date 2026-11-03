"""Upload resolves MIME from filename when Content-Type is missing, rejects the unknown."""

from __future__ import annotations

import types
import uuid

import pytest
from app.documents import upload as up
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


class _Session:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    async def flush(self):
        for o in self.added:
            if getattr(o, "id", None) is None:
                o.id = uuid.uuid4()

    async def commit(self):
        await self.flush()

    async def refresh(self, obj):
        pass


def _client(monkeypatch, *, existing=None, latest=None, latest_job=None):
    session = _Session()
    app = FastAPI()
    app.include_router(up.router)
    app.dependency_overrides[up.get_session] = lambda: session
    app.dependency_overrides[up.get_principal] = lambda: object()
    monkeypatch.setattr(up, "require", lambda *a, **k: None)

    async def _find(*a, **k):
        return existing

    async def _latest(*a, **k):
        return latest

    async def _job(*a, **k):
        return latest_job

    async def _put(**k):
        return "key"

    async def _enqueue(_job_id):
        return None

    monkeypatch.setattr(up, "find_document_by_filename", _find)
    monkeypatch.setattr(up, "latest_version", _latest)
    monkeypatch.setattr(up, "latest_job_for_version", _job)
    monkeypatch.setattr(up, "put_original", _put)
    monkeypatch.setattr(up, "enqueue", _enqueue)
    return TestClient(app), session


def _post(client, name, ctype):
    files = {"file": (name, b"hello", ctype)} if ctype else {"file": (name, b"hello")}
    return client.post(f"/v1/workspaces/{uuid.uuid4()}/documents", files=files)


def test_untyped_unknown_extension_is_rejected_synchronously(monkeypatch):
    client, session = _client(monkeypatch)
    r = _post(client, "blob.xyz", None)
    assert r.status_code == 415
    assert r.json()["detail"]["error"] == "UNSUPPORTED_MIME_TYPE"
    assert session.added == []


def test_untyped_known_extension_gets_guessed_mime(monkeypatch):
    client, session = _client(monkeypatch)
    r = _post(client, "note.TXT", "application/octet-stream")
    assert r.status_code == 202
    version = next(o for o in session.added if isinstance(o, up.DocumentVersion))
    assert version.mime_type == "text/plain"


def test_declared_mime_params_are_stripped(monkeypatch):
    assert up._resolve_mime("text/html; charset=utf-8", "x.dat") == "text/html"


def test_same_bytes_reupload_refreshes_poisoned_mime(monkeypatch):
    import hashlib

    doc = types.SimpleNamespace(id=uuid.uuid4())
    latest = types.SimpleNamespace(
        id=uuid.uuid4(),
        checksum=hashlib.sha256(b"hello").hexdigest(),
        version_number=1,
        mime_type=None,
    )
    failed = types.SimpleNamespace(id=uuid.uuid4(), status="failed")
    client, _ = _client(monkeypatch, existing=doc, latest=latest, latest_job=failed)
    r = _post(client, "note.txt", None)
    assert r.status_code == 202
    assert latest.mime_type == "text/plain"
