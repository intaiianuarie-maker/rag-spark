"""Source CRUD and authorization using a private schema and real migration 0015."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.auth.db import get_session
from app.authz.principal import get_principal
from app.sources.router import router
from connectors.adapters.config_loader import load_persisted_sources
from connectors.adapters.progress_store import SyncOutcomeRow, SyncProgressRow
from core.auth import Principal, Role
from core.config import get_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def restore_local_root_policy():
    previous = os.environ.get("CONNECTOR_LOCAL_ROOTS")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CONNECTOR_LOCAL_ROOTS", None)
        else:
            os.environ["CONNECTOR_LOCAL_ROOTS"] = previous


@pytest.fixture(scope="module")
def client():
    schema = "sources_test_" + uuid.uuid4().hex
    url = get_settings().database_url.replace("+asyncpg", "+psycopg")
    admin = create_engine(url, connect_args={"connect_timeout": 3}, poolclass=NullPool)
    try:
        with admin.connect():
            pass
    except OperationalError:
        admin.dispose()
        pytest.skip("PostgreSQL unavailable; start compose postgres for isolated Sources tests")
    options = {"options": f"-csearch_path={schema}", "connect_timeout": 3}
    setup_engine = create_engine(url, connect_args=options, poolclass=NullPool)
    engine = create_async_engine(url, connect_args=options, poolclass=NullPool)
    ws, other = uuid.uuid4(), uuid.uuid4()
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        with setup_engine.begin() as conn:
            conn.execute(text("CREATE TABLE workspaces (id uuid PRIMARY KEY)"))
            conn.execute(
                text("INSERT INTO workspaces (id) VALUES (:a), (:b)"), {"a": ws, "b": other}
            )
            path = Path(__file__).parents[2] / "apps/api/alembic/versions/0015_connector_configs.py"
            spec = importlib.util.spec_from_file_location("sources_migration", path)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            migration.op = Operations(MigrationContext.configure(conn))
            migration.upgrade()  # migration defects fail; only service absence skips
            SyncProgressRow.__table__.create(conn)
            SyncOutcomeRow.__table__.create(conn)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def session_dependency():
            async with sessions() as session:
                yield session

        principal = Principal(
            user_id="fixture-user",
            is_authenticated=True,
            workspace_roles={str(ws): Role.OWNER, str(other): Role.OWNER},
        )
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_session] = session_dependency
        app.dependency_overrides[get_principal] = lambda: principal
        with TestClient(app) as c:
            c.ws, c.other, c.principal = ws, other, principal
            c.sync_sessions = lambda: Session(setup_engine)
            c.load_sources = lambda workspace: load_persisted_sources(
                str(workspace), session_factory=c.sync_sessions
            )
            yield c
    finally:
        asyncio.run(engine.dispose())
        setup_engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def test_sources_crud_and_status(client) -> None:
    ws = client.ws
    prefix = f"/v1/workspaces/{ws}/sources"

    r = client.post(
        prefix,
        json={
            "connector_id": "s3docs",
            "kind": "s3",
            "settings": {"bucket": "b", "prefix": "docs/"},
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["connector_id"] == "s3docs"

    r = client.post(
        prefix, json={"connector_id": "s3docs", "kind": "s3", "settings": {"bucket": "b"}}
    )
    assert r.status_code == 409

    r = client.get(prefix)
    assert r.status_code == 200
    assert len(r.json()) == 1

    r = client.get(f"{prefix}/s3docs")
    assert r.status_code == 200

    r = client.patch(f"{prefix}/s3docs", json={"settings": {"bucket": "b2", "prefix": "x/"}})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["bucket"] == "b2"

    r = client.patch(f"{prefix}/s3docs", json={"settings": {"bucket": ""}})
    assert r.status_code == 422

    r = client.post(f"{prefix}/s3docs/sync")
    assert r.status_code in (200, 503)

    r = client.get(f"{prefix}/s3docs/status")
    assert r.status_code == 200
    assert r.json()["connector_id"] == "s3docs"

    r = client.delete(f"{prefix}/s3docs")
    assert r.status_code == 204
    r = client.get(f"{prefix}/s3docs")
    assert r.status_code == 404


def test_sources_validation_boundaries(client) -> None:
    ws = client.ws
    prefix = f"/v1/workspaces/{ws}/sources"
    import os

    os.environ["CONNECTOR_LOCAL_ROOTS"] = "/tmp"
    # traversal rejected
    r = client.post(
        prefix,
        json={
            "connector_id": "local1",
            "kind": "local_upload",
            "settings": {"path": "/tmp/../etc"},
        },
    )
    assert r.status_code == 422, r.text
    # credential_ref rejected
    r = client.post(
        prefix,
        json={
            "connector_id": "s3cred",
            "kind": "s3",
            "settings": {"bucket": "b"},
            "credential_ref": "SECRET",
        },
    )
    assert r.status_code == 422, r.text
    # endpoint_ref rejected
    r = client.post(
        prefix,
        json={
            "connector_id": "s3ep",
            "kind": "s3",
            "settings": {"bucket": "b", "endpoint_ref": "EP"},
        },
    )
    assert r.status_code == 422, r.text
    # no roots configured -> fail closed
    del os.environ["CONNECTOR_LOCAL_ROOTS"]
    r = client.post(
        prefix,
        json={"connector_id": "local2", "kind": "local_upload", "settings": {"path": "/tmp/x"}},
    )
    assert r.status_code == 422, r.text


def test_sources_create_all_four_types(client) -> None:
    ws = client.ws
    prefix = f"/v1/workspaces/{ws}/sources"
    os.environ["CONNECTOR_LOCAL_ROOTS"] = "/tmp"
    cases = [
        ("local1", "local_upload", {"path": "/tmp"}),
        ("web1", "web", {"urls": ["https://example.com"]}),
        ("s31", "s3", {"bucket": "b"}),
        ("git1", "git", {"repo": "https://example.com/r.git"}),
    ]
    for cid, kind, settings in cases:
        r = client.post(prefix, json={"connector_id": cid, "kind": kind, "settings": settings})
        assert r.status_code == 201, (cid, r.text)
        assert r.json()["kind"] == kind
        # credential_ref never echoed as a secret value
        assert "secret" not in str(r.json()).lower()
    # cleanup
    for cid, _, _ in cases:
        client.delete(f"{prefix}/{cid}")


def test_sources_invalid_kind_and_missing_fields(client) -> None:
    ws = client.ws
    prefix = f"/v1/workspaces/{ws}/sources"
    r = client.post(prefix, json={"connector_id": "bad", "kind": "ftp", "settings": {}})
    assert r.status_code == 422
    r = client.post(prefix, json={"connector_id": "bad2", "kind": "web", "settings": {}})
    assert r.status_code == 422
    r = client.post(prefix, json={"connector_id": "", "kind": "s3", "settings": {"bucket": "b"}})
    assert r.status_code == 422


def test_sources_credential_ref_rejected(client) -> None:
    ws = client.ws
    prefix = f"/v1/workspaces/{ws}/sources"
    r = client.post(
        prefix,
        json={
            "connector_id": "cred1",
            "kind": "s3",
            "settings": {"bucket": "b"},
            "credential_ref": "MY_SECRET",
        },
    )
    assert r.status_code == 422, r.text
    # ensure not persisted
    r = client.get(f"{prefix}/cred1")
    assert r.status_code == 404


def test_sources_status_and_delete(client) -> None:
    ws = client.ws
    prefix = f"/v1/workspaces/{ws}/sources"
    os.environ["CONNECTOR_LOCAL_ROOTS"] = "/tmp"
    r = client.post(
        prefix, json={"connector_id": "st1", "kind": "local_upload", "settings": {"path": "/tmp"}}
    )
    assert r.status_code == 201
    r = client.get(f"{prefix}/st1/status")
    assert r.status_code == 200
    body = r.json()
    assert body["connector_id"] == "st1"
    assert body["items_seen"] == 0
    r = client.delete(f"{prefix}/st1")
    assert r.status_code == 204


def test_sources_trigger_secret_redaction(client, monkeypatch) -> None:
    ws = client.ws
    prefix = f"/v1/workspaces/{ws}/sources"
    os.environ["CONNECTOR_LOCAL_ROOTS"] = "/tmp"
    r = client.post(
        prefix,
        json={"connector_id": "redact1", "kind": "local_upload", "settings": {"path": "/tmp"}},
    )
    assert r.status_code == 201

    import app.sources.router as router_mod

    async def boom(*a, **k):
        raise RuntimeError("secret-token-123 leaked")

    monkeypatch.setattr(router_mod, "enqueue_sync_job", boom)
    r = client.post(f"{prefix}/redact1/sync")
    assert r.status_code == 503
    assert "secret-token-123" not in r.text
    assert r.json()["detail"] == "sync queue unavailable"
    client.delete(f"{prefix}/redact1")


def test_web_settings_real_adapter_ms_only(client) -> None:
    ws = client.ws
    prefix = f"/v1/workspaces/{ws}/sources"
    r = client.post(
        prefix,
        json={
            "connector_id": "webms",
            "kind": "web",
            "settings": {"urls": ["https://example.com"], "politeness_delay_ms": 250},
        },
    )
    assert r.status_code == 201, r.text
    settings = r.json()["settings"]
    assert "politeness_delay" not in settings  # exclude_none
    assert settings.get("politeness_delay_ms") == 250
    from connectors import ConnectorConfig, ConnectorKind
    from connectors.adapters.web_connector import WebConnector

    cfg = ConnectorConfig(
        connector_id="webms", workspace_id=str(ws), kind=ConnectorKind.WEB, settings=settings
    )
    adapter = WebConnector()
    delay = adapter._resolve_politeness_delay(cfg)
    assert delay == pytest.approx(0.25)
    client.delete(f"{prefix}/webms")


def test_all_four_settings_instantiate_adapters(client) -> None:
    ws = client.ws
    prefix = f"/v1/workspaces/{ws}/sources"
    os.environ["CONNECTOR_LOCAL_ROOTS"] = "/tmp"
    cases = [
        ("l1", "local_upload", {"path": "/tmp"}),
        ("w1", "web", {"urls": ["https://example.com"]}),
        ("s1", "s3", {"bucket": "b"}),
        ("g1", "git", {"repo": "/tmp"}),
    ]
    from connectors import ConnectorConfig, ConnectorKind, create_connector

    for cid, kind, settings in cases:
        r = client.post(prefix, json={"connector_id": cid, "kind": kind, "settings": settings})
        assert r.status_code == 201, (cid, r.text)
        cfg = ConnectorConfig(
            connector_id=cid,
            workspace_id=str(ws),
            kind=ConnectorKind(kind),
            settings=r.json()["settings"],
        )
        adapter = create_connector(cfg)
        assert adapter is not None
        client.delete(f"{prefix}/{cid}")


def test_sources_git_web_userinfo_rejected(client) -> None:
    ws = client.ws
    prefix = f"/v1/workspaces/{ws}/sources"
    os.environ["CONNECTOR_LOCAL_ROOTS"] = "/tmp"
    r = client.post(
        prefix,
        json={
            "connector_id": "gitbad",
            "kind": "git",
            "settings": {"repo": "https://user:secret@host/repo.git"},
        },
    )
    assert r.status_code == 422, r.text
    assert "secret" not in r.text
    r = client.post(
        prefix,
        json={
            "connector_id": "webbad",
            "kind": "web",
            "settings": {"urls": ["https://user:secret@example.com"]},
        },
    )
    assert r.status_code == 422, r.text
    assert "secret" not in r.text


def test_sources_roles_and_foreign_connector_ids(client, monkeypatch):
    prefix = f"/v1/workspaces/{client.ws}/sources"
    foreign = f"/v1/workspaces/{client.other}/sources/acl-control"
    assert (
        client.post(
            prefix,
            json={
                "connector_id": "acl-control",
                "kind": "s3",
                "settings": {"bucket": "public-fixture"},
            },
        ).status_code
        == 201
    )
    try:
        # A legitimate owner of both workspaces still cannot resolve A's source in B.
        for suffix in ("", "/status"):
            assert client.get(foreign + suffix).status_code == 404
        assert client.patch(foreign, json={"settings": {"bucket": "changed"}}).status_code == 404
        assert client.delete(foreign).status_code == 404
        assert client.post(foreign + "/sync").status_code == 404
        client.principal.workspace_roles[str(client.ws)] = Role.VIEWER
        assert client.get(prefix).status_code == 200
        assert client.get(prefix + "/acl-control/status").status_code == 200
        for method, suffix, body in (
            ("post", "", {"connector_id": "denied", "kind": "s3", "settings": {"bucket": "b"}}),
            ("patch", "/acl-control", {"settings": {"bucket": "changed"}}),
            ("delete", "/acl-control", None),
            ("post", "/acl-control/sync", None),
        ):
            kwargs = {"json": body} if body is not None else {}
            assert getattr(client, method)(prefix + suffix, **kwargs).status_code == 403
        unauthorized = f"/v1/workspaces/{uuid.uuid4()}/sources"
        assert client.get(unauthorized).status_code == 403
        assert (
            client.post(
                unauthorized, json={"connector_id": "x", "kind": "s3", "settings": {"bucket": "b"}}
            ).status_code
            == 403
        )
    finally:
        client.principal.workspace_roles[str(client.ws)] = Role.OWNER
        assert client.delete(prefix + "/acl-control").status_code == 204


def test_connector_id_path_safe(client) -> None:
    ws = client.ws
    prefix = f"/v1/workspaces/{ws}/sources"
    # valid
    r = client.post(
        prefix, json={"connector_id": "a.b-c_1", "kind": "s3", "settings": {"bucket": "b"}}
    )
    assert r.status_code == 201, r.text
    client.delete(f"{prefix}/a.b-c_1")
    # slash
    r = client.post(prefix, json={"connector_id": "a/b", "kind": "s3", "settings": {"bucket": "b"}})
    assert r.status_code == 422
    # traversal
    r = client.post(
        prefix, json={"connector_id": "../x", "kind": "s3", "settings": {"bucket": "b"}}
    )
    assert r.status_code == 422
    # empty
    r = client.post(prefix, json={"connector_id": "", "kind": "s3", "settings": {"bucket": "b"}})
    assert r.status_code == 422


def test_persisted_worker_loader_revalidates_scope_and_changed_root_policy(client, tmp_path,
                                                                         monkeypatch):
    approved = tmp_path / "approved"
    approved.mkdir()
    monkeypatch.setenv("CONNECTOR_LOCAL_ROOTS", str(approved))
    prefix = f"/v1/workspaces/{client.ws}/sources"
    body = {"connector_id": "runtime-root", "kind": "git", "settings": {"repo": approved.as_uri()}}
    assert client.post(prefix, json=body).status_code == 201
    try:
        loaded = client.load_sources(client.ws)
        item = next(config for config in loaded if config.connector_id == "runtime-root")
        assert item.settings["repo"] == str(approved)
        assert item.workspace_id == str(client.ws)
        assert all(config.connector_id != "runtime-root"
                   for config in client.load_sources(client.other))
        monkeypatch.setenv("CONNECTOR_LOCAL_ROOTS", str(tmp_path / "different"))
        with pytest.raises(ValueError, match="approved root"):
            client.load_sources(client.ws)
    finally:
        assert client.delete(prefix + "/runtime-root").status_code == 204


def test_worker_loader_refuses_legacy_credential_payload_without_traceback_leak(client):
    import json
    import traceback

    prefix = f"/v1/workspaces/{client.ws}/sources"
    created = client.post(prefix, json={"connector_id": "legacy-config", "kind": "web",
                                        "settings": {"urls": ["https://example.com"]}})
    assert created.status_code == 201
    try:
        with client.sync_sessions() as session:
            session.execute(text("UPDATE connector_configs SET settings = CAST(:settings AS json) "
                                 "WHERE workspace_id = :workspace AND connector_id = :connector"),
                            {"workspace": client.ws, "connector": "legacy-config",
                             "settings": json.dumps({"urls": ["https://private-sentinel@example.com"]})})
            session.commit()
        with pytest.raises(ValueError) as caught:
            client.load_sources(client.ws)
        assert "private-sentinel" not in "".join(traceback.format_exception(caught.value))
    finally:
        assert client.delete(prefix + "/legacy-config").status_code == 204
