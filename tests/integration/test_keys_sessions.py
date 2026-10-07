"""Integration tests for API keys + Redis sessions (M1-001c).

Requires postgres + redis; skips with an explicit message when they are down.
Applies ``alembic upgrade head`` (idempotent) before the first test.
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import UTC, datetime, timedelta

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


def _new_session_user(client) -> tuple[str, str, str]:
    """Register + login; return (access_token, refresh_token, user_id)."""
    email = f"m1c-{uuid.uuid4().hex}@example.test"
    response = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    user_id = response.json()["id"]
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    body = login.json()
    return body["access_token"], body["refresh_token"], user_id


def _create_workspace(client, token: str, name: str) -> str:
    response = client.post("/v1/workspaces", json={"name": name}, headers=_auth(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _issue_key(client, token: str, workspace_id: str, **overrides) -> dict:
    payload = {"name": "test-key", "role": "viewer"}
    payload.update(overrides)
    response = client.post(
        f"/v1/workspaces/{workspace_id}/keys", json=payload, headers=_auth(token)
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_api_key_scope_revocation_and_no_secret_on_list(client) -> None:
    a_token, _a_refresh, _a_id = _new_session_user(client)
    workspace_a = _create_workspace(client, a_token, "Key A")

    created = _issue_key(client, a_token, workspace_a)
    full_key = created["key"]
    key_id = created["id"]
    assert full_key.startswith("kio_")
    assert created["prefix"] in full_key

    # List never exposes the secret or the hash.
    listed = client.get(
        f"/v1/workspaces/{workspace_a}/keys", headers=_auth(a_token)
    )
    assert listed.status_code == 200
    assert any(item["id"] == key_id for item in listed.json())
    assert all("key" not in item and "key_hash" not in item for item in listed.json())

    # The key can read its own workspace and list only that workspace.
    assert client.get(f"/v1/workspaces/{workspace_a}", headers=_auth(full_key)).status_code == 200
    scoped = client.get("/v1/workspaces", headers=_auth(full_key)).json()
    assert [workspace["id"] for workspace in scoped] == [workspace_a]

    # A key cannot create workspaces or manage keys with a viewer role.
    assert (
        client.post("/v1/workspaces", json={"name": "nope"}, headers=_auth(full_key)).status_code
        == 403
    )
    assert (
        client.get(f"/v1/workspaces/{workspace_a}/keys", headers=_auth(full_key)).status_code
        == 403
    )

    # Workspace-A key is rejected for workspace B (cross-workspace).
    b_token, _b_refresh, _b_id = _new_session_user(client)
    workspace_b = _create_workspace(client, b_token, "Key B")
    assert client.get(f"/v1/workspaces/{workspace_b}", headers=_auth(full_key)).status_code == 403
    assert (
        client.patch(
            f"/v1/workspaces/{workspace_b}", json={"name": "hax"}, headers=_auth(full_key)
        ).status_code
        == 403
    )

    # Revoke -> the key is rejected.
    assert (
        client.delete(
            f"/v1/workspaces/{workspace_a}/keys/{key_id}", headers=_auth(a_token)
        ).status_code
        == 204
    )
    assert client.get(f"/v1/workspaces/{workspace_a}", headers=_auth(full_key)).status_code == 401


def test_expired_key_is_rejected(client) -> None:
    token, _refresh, _uid = _new_session_user(client)
    workspace_id = _create_workspace(client, token, "Expiring")
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    created = _issue_key(client, token, workspace_id, name="old", expires_at=past)
    assert client.get(
        f"/v1/workspaces/{workspace_id}", headers=_auth(created["key"])
    ).status_code == 401


def test_session_revoked_on_logout_rejects_access_token(client) -> None:
    access, refresh, _uid = _new_session_user(client)
    workspace_id = _create_workspace(client, access, "Session")
    assert client.get("/v1/workspaces", headers=_auth(access)).status_code == 200

    assert client.post("/auth/logout", json={"refresh_token": refresh}).status_code == 204

    # Server-side session revocation: the (unexpired) access token is now 401.
    assert client.get("/v1/workspaces", headers=_auth(access)).status_code == 401
    assert client.get(f"/v1/workspaces/{workspace_id}", headers=_auth(access)).status_code == 401
    # Refresh token is also dead.
    assert client.post("/auth/refresh", json={"refresh_token": refresh}).status_code == 401


def test_bogus_credentials_are_401(client) -> None:
    assert (
        client.get(
            "/v1/workspaces", headers=_auth("kio_deadbeef_notarealsecret")
        ).status_code
        == 401
    )
    assert client.get("/v1/workspaces", headers=_auth("not-a-jwt")).status_code == 401
