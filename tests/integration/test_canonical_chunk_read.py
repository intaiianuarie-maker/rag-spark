"""Canonical passage reads against real SQL in an isolated ingestion schema."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from app.auth.db import get_session
from app.authz.principal import get_principal
from app.documents.chunks import router
from core.auth import Principal, Role
from core.config import get_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ingestion.persistence import Base, ChunkRow, Document, DocumentVersion
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration


def _principal(workspace, role=Role.VIEWER):
    return Principal(user_id="passage-fixture", is_authenticated=True,
                     workspace_roles={str(workspace): role})


@pytest.fixture(scope="module")
def client():
    schema = "canonical_chunk_test_" + uuid.uuid4().hex
    url = get_settings().database_url.replace("+asyncpg", "+psycopg")
    admin = create_engine(url, connect_args={"connect_timeout": 3}, poolclass=NullPool)
    try:
        with admin.connect():
            pass
    except OperationalError:
        admin.dispose()
        pytest.skip("PostgreSQL unavailable; canonical read requires isolated PostgreSQL schema")
    options = {"options": f"-csearch_path={schema}", "connect_timeout": 3}
    setup = create_engine(url, connect_args=options, poolclass=NullPool)
    engine = create_async_engine(url, connect_args=options, poolclass=NullPool)
    workspace, other = uuid.uuid4(), uuid.uuid4()
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        with setup.begin() as connection:
            Base.metadata.create_all(connection)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def dependency():
            async with sessions() as session:
                yield session

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_session] = dependency
        app.dependency_overrides[get_principal] = lambda: _principal(workspace)
        with TestClient(app) as c:
            c.workspace, c.other = workspace, other
            c.sessions = lambda: Session(setup)
            c.app_fixture = app
            yield c
    finally:
        asyncio.run(engine.dispose())
        setup.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def _seed(client, *, chunk_workspace=None, document_workspace=None,
          deleted=False, superseded=False, current=True, content="Canonical stored passage.",
          metadata=None):
    with client.sessions() as session:
        document = Document(workspace_id=document_workspace or client.workspace,
                            source_filename="canonical.txt")
        session.add(document)
        session.flush()
        version = DocumentVersion(document_id=document.id, checksum="a" * 64,
                                  size_bytes=30, version_number=1)
        session.add(version)
        session.flush()
        document.current_version_id = version.id if current else None
        if deleted:
            document.deleted_at = datetime.now(UTC)
        if superseded:
            version.superseded_at = datetime.now(UTC)
        chunk = ChunkRow(workspace_id=chunk_workspace or client.workspace,
                         document_version_id=version.id, chunk_index=0, text=content,
                         token_count=4, page=3, section="Terms",
                         chunk_metadata=metadata if metadata is not None else {
                             "span": [0, 9], "bbox": [1, 2, 3, 4],
                             "private": "not a response field"})
        session.add(chunk)
        session.flush()
        ids = chunk.id, document.id, version.id
        session.commit()
        return ids


def _url(workspace, chunk):
    return f"/v1/workspaces/{workspace}/chunks/{chunk}"


@pytest.mark.parametrize("role", list(Role))
def test_canonical_current_chunk_has_bff_fields_and_read_roles(client, role):
    chunk, document, _ = _seed(client)
    app = client.app_fixture
    app.dependency_overrides[get_principal] = lambda: _principal(client.workspace, role)
    try:
        response = client.get(_url(client.workspace, chunk))
    finally:
        app.dependency_overrides[get_principal] = lambda: _principal(client.workspace)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "chunk_id": str(chunk), "document_id": str(document),
        "workspace_id": str(client.workspace), "filename": "canonical.txt",
        "page": 3, "section": "Terms", "content": "Canonical stored passage.",
        "span": [0, 9], "bbox": [1, 2, 3, 4],
    }


@pytest.mark.parametrize("case", ["missing", "foreign", "chunk_foreign", "document_foreign",
                                  "deleted", "superseded", "not_current", "empty"])
def test_unavailable_or_inconsistent_rows_have_identical_404(client, case):
    args = {}
    if case in {"foreign", "chunk_foreign"}:
        args["chunk_workspace"] = client.other
    if case in {"foreign", "document_foreign"}:
        args["document_workspace"] = client.other
    if case in {"deleted", "superseded"}:
        args[case] = True
    if case == "not_current":
        args["current"] = False
    if case == "empty":
        args["content"] = ""
    chunk, _, _ = _seed(client, **args)
    if case == "missing":
        chunk = uuid.uuid4()
    response = client.get(_url(client.workspace, chunk))
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": {"error": "chunk_not_found",
                                          "message": "Passage not found."}}


def test_old_version_is_refused_even_without_superseded_marker(client):
    old, document_id, _ = _seed(client)
    with client.sessions() as session:
        version = DocumentVersion(document_id=document_id, version_number=2,
                                  checksum="b" * 64, size_bytes=20)
        session.add(version)
        session.flush()
        session.get(Document, document_id).current_version_id = version.id
        chunk = ChunkRow(workspace_id=client.workspace, document_version_id=version.id,
                         chunk_index=0, text="New canonical text.", token_count=3)
        session.add(chunk)
        session.flush()
        new = chunk.id
        session.commit()
    assert client.get(_url(client.workspace, old)).status_code == 404
    response = client.get(_url(client.workspace, new))
    assert response.status_code == 200
    assert response.json()["content"] == "New canonical text."
    assert response.json()["span"] is response.json()["bbox"] is None


@pytest.mark.parametrize("content", [" ", "\t\r\n", "\u00a0\u2003\u2028"])
def test_whitespace_only_canonical_content_is_unavailable(client, content):
    chunk, _, _ = _seed(client, content=content)
    response = client.get(_url(client.workspace, chunk))
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "chunk_not_found"


@pytest.mark.parametrize("span", [[0.0, 2.0], [False, 2], [-1, 2], [5, 2], [0, 1000],
                                  [0], "0,2", {"start": 0, "end": 2}])
def test_invalid_character_spans_are_not_returned(client, span):
    chunk, _, _ = _seed(client, metadata={"span": span})
    response = client.get(_url(client.workspace, chunk))
    assert response.status_code == 200
    assert response.json()["span"] is None


@pytest.mark.parametrize("span", [[0, 0], [0, 4], [2, 4]])
def test_valid_character_span_uses_integer_offsets_within_real_text(client, span):
    chunk, _, _ = _seed(client, content="Text", metadata={"span": span})
    response = client.get(_url(client.workspace, chunk))
    assert response.status_code == 200
    assert response.json()["span"] == span
    assert all(type(offset) is int for offset in response.json()["span"])


@pytest.mark.parametrize("metadata", [[], ["forged text"], "forged text", 42])
def test_malformed_metadata_does_not_hide_or_replace_real_text(client, metadata):
    chunk, _, _ = _seed(client, content="Real persisted text.", metadata=metadata)
    response = client.get(_url(client.workspace, chunk))
    assert response.status_code == 200
    assert response.json()["content"] == "Real persisted text."
    assert response.json()["span"] is response.json()["bbox"] is None


def test_metadata_cannot_forge_passage_text_or_document_provenance(client):
    chunk, document, _ = _seed(client, content="Authoritative SQL text.", metadata={
        "content": "Forged metadata text.", "text": "Another forged text.",
        "chunk_id": str(uuid.uuid4()), "document_id": str(uuid.uuid4()),
        "workspace_id": str(client.other), "filename": "forged.txt",
    })
    response = client.get(_url(client.workspace, chunk))
    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "Authoritative SQL text."
    assert body["document_id"] == str(document)
    assert body["chunk_id"] == str(chunk)
    assert body["workspace_id"] == str(client.workspace)
    assert body["filename"] == "canonical.txt"
    assert "Forged" not in response.text


@pytest.mark.parametrize("case", ["nonmember", "inactive", "anonymous"])
def test_authorization_denies_before_any_chunk_query(case):
    workspace = uuid.uuid4()
    session = AsyncMock()
    session.execute.side_effect = AssertionError("denied requests must not query chunks")

    async def dependency():
        yield session

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = dependency
    principal = _principal(workspace)
    if case == "nonmember":
        principal.workspace_roles = {}
    elif case == "inactive":
        principal.is_active = False
    if case != "anonymous":
        app.dependency_overrides[get_principal] = lambda: principal
    with TestClient(app) as c:
        response = c.get(_url(workspace, uuid.uuid4()))
    assert response.status_code == (401 if case == "anonymous" else 403)
    session.execute.assert_not_awaited()


def test_store_failure_does_not_echo_vendor_details():
    workspace = uuid.uuid4()
    session = AsyncMock()
    session.execute.side_effect = SQLAlchemyError("private-vendor-error")

    async def dependency():
        yield session

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = dependency
    app.dependency_overrides[get_principal] = lambda: _principal(workspace)
    with TestClient(app) as c:
        response = c.get(_url(workspace, uuid.uuid4()))
    assert response.status_code == 503
    assert "private-vendor-error" not in response.text
