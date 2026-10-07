"""Integration tests for the workspace/membership lifecycle (M1-001b).

Requires postgres + redis (compose up); skips with an explicit message when the
services are down. Applies ``alembic upgrade head`` (idempotent) before the
first test so 0003_workspaces is present.
"""

from __future__ import annotations

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


def _new_user(client) -> tuple[str, str, str]:
    """Register + login a fresh user; return (email, access_token, user_id)."""
    email = f"m1b-{uuid.uuid4().hex}@example.test"
    response = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    user_id = response.json()["id"]
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return email, response.json()["access_token"], user_id


def _auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _create_workspace(client, token: str, name: str = "Alpha") -> str:
    response = client.post("/v1/workspaces", json={"name": name}, headers=_auth(token))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["role"] == "owner"
    return body["id"]


def test_workspace_lifecycle_invite_accept_change_remove(client) -> None:
    _a_email, a_token, _ = _new_user(client)
    b_email, b_token, _ = _new_user(client)
    workspace_id = _create_workspace(client, a_token, "Alpha")

    # Creator sees it; the future invitee does not.
    assert any(
        w["id"] == workspace_id
        for w in client.get("/v1/workspaces", headers=_auth(a_token)).json()
    )
    assert all(
        w["id"] != workspace_id
        for w in client.get("/v1/workspaces", headers=_auth(b_token)).json()
    )

    # Invite B as editor -> invited (not yet active).
    response = client.post(
        f"/v1/workspaces/{workspace_id}/members",
        json={"email": b_email, "role": "editor"},
        headers=_auth(a_token),
    )
    assert response.status_code == 201, response.text
    member = response.json()
    assert member["status"] == "invited"
    assert member["role"] == "editor"
    b_user_id = member["user_id"]

    # Invited user has no access yet.
    assert (
        client.get(f"/v1/workspaces/{workspace_id}", headers=_auth(b_token)).status_code
        == 403
    )

    # Accept -> active and visible.
    response = client.post(
        f"/v1/workspaces/{workspace_id}/members/accept", headers=_auth(b_token)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    assert any(
        w["id"] == workspace_id
        for w in client.get("/v1/workspaces", headers=_auth(b_token)).json()
    )

    # Owner renames and changes B's role.
    response = client.patch(
        f"/v1/workspaces/{workspace_id}",
        json={"name": "Alpha Renamed"},
        headers=_auth(a_token),
    )
    assert response.status_code == 200 and response.json()["name"] == "Alpha Renamed"

    response = client.patch(
        f"/v1/workspaces/{workspace_id}/members/{b_user_id}",
        json={"role": "viewer"},
        headers=_auth(a_token),
    )
    assert response.status_code == 200 and response.json()["role"] == "viewer"

    # Duplicate invite is a conflict.
    assert (
        client.post(
            f"/v1/workspaces/{workspace_id}/members",
            json={"email": b_email, "role": "viewer"},
            headers=_auth(a_token),
        ).status_code
        == 409
    )

    # Remove B -> 204 and access revoked.
    response = client.delete(
        f"/v1/workspaces/{workspace_id}/members/{b_user_id}", headers=_auth(a_token)
    )
    assert response.status_code == 204
    assert (
        client.get(f"/v1/workspaces/{workspace_id}", headers=_auth(b_token)).status_code
        == 403
    )


def test_non_member_gets_403_across_workspaces(client) -> None:
    _a_email, a_token, _ = _new_user(client)
    c_email, c_token, c_user_id = _new_user(client)
    a_workspace = _create_workspace(client, a_token, "A-only")
    _create_workspace(client, c_token, "C-only")  # C has their own workspace

    assert (
        client.get(f"/v1/workspaces/{a_workspace}", headers=_auth(c_token)).status_code
        == 403
    )
    assert (
        client.patch(
            f"/v1/workspaces/{a_workspace}",
            json={"name": "hax"},
            headers=_auth(c_token),
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/workspaces/{a_workspace}/members",
            json={"email": c_email, "role": "admin"},
            headers=_auth(c_token),
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/v1/workspaces/{a_workspace}/members/{c_user_id}",
            headers=_auth(c_token),
        ).status_code
        == 403
    )


def test_last_owner_cannot_lock_themselves_out(client) -> None:
    _email, token, user_id = _new_user(client)
    workspace_id = _create_workspace(client, token, "Solo")

    demote = client.patch(
        f"/v1/workspaces/{workspace_id}/members/{user_id}",
        json={"role": "admin"},
        headers=_auth(token),
    )
    assert demote.status_code == 409
    assert demote.json()["detail"]["error"] == "last_owner"

    leave = client.delete(
        f"/v1/workspaces/{workspace_id}/members/{user_id}", headers=_auth(token)
    )
    assert leave.status_code == 409
    assert leave.json()["detail"]["error"] == "last_owner"


def test_invite_unknown_user_is_404(client) -> None:
    _email, token, _ = _new_user(client)
    workspace_id = _create_workspace(client, token, "Invites")
    response = client.post(
        f"/v1/workspaces/{workspace_id}/members",
        json={"email": f"nobody-{uuid.uuid4().hex}@example.test", "role": "viewer"},
        headers=_auth(token),
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "user_not_found"


def test_workspace_endpoints_require_bearer_token(client) -> None:
    assert client.get("/v1/workspaces").status_code == 401
    assert client.post("/v1/workspaces", json={"name": "x"}).status_code == 401
    assert (
        client.get(
            f"/v1/workspaces/{uuid.uuid4()}", headers={"authorization": "Bearer not-a-jwt"}
        ).status_code
        == 401
    )


def test_invalid_uuid_path_uses_lean_422(client) -> None:
    _email, token, _ = _new_user(client)
    response = client.get("/v1/workspaces/not-a-uuid", headers=_auth(token))
    assert response.status_code == 422
    assert "input" not in response.text
