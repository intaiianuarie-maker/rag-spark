"""Integration: M1-001f member roster + my-invitations (frozen contract)."""

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
ROSTER_FIELDS = {"user_id", "email", "role", "status"}
INVITATION_FIELDS = {"workspace_id", "workspace_name", "role", "invited_at"}


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


def _new_user(client) -> tuple[str, str, str]:
    email = f"m1f-{uuid.uuid4().hex}@example.test"
    registered = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return email, login.json()["access_token"], registered.json()["id"]


def _create_workspace(client, token: str) -> str:
    response = client.post("/v1/workspaces", json={"name": "Roster"}, headers=_auth(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _invite(client, token: str, workspace_id: str, email: str, role: str) -> dict:
    response = client.post(
        f"/v1/workspaces/{workspace_id}/members",
        json={"email": email, "role": role},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _roster(client, token: str, workspace_id: str) -> dict[str, dict]:
    response = client.get(
        f"/v1/workspaces/{workspace_id}/members", headers=_auth(token)
    )
    assert response.status_code == 200, response.text
    return {item["email"]: item for item in response.json()}


def test_roster_includes_invited_and_members_and_enforces_membership(client) -> None:
    a_email, a_token, _ = _new_user(client)
    b_email, b_token, _ = _new_user(client)
    c_email, c_token, _ = _new_user(client)
    d_email, d_token, _ = _new_user(client)
    workspace = _create_workspace(client, a_token)
    _invite(client, a_token, workspace, b_email, "editor")
    _invite(client, a_token, workspace, c_email, "viewer")
    assert (
        client.post(
            f"/v1/workspaces/{workspace}/members/accept", headers=_auth(b_token)
        ).status_code
        == 200
    )

    roster = _roster(client, a_token, workspace)
    assert all(set(item) == ROSTER_FIELDS for item in roster.values())
    assert roster[a_email]["role"] == "owner" and roster[a_email]["status"] == "active"
    assert roster[b_email]["role"] == "editor" and roster[b_email]["status"] == "active"
    # Invited (pre-accept) members are included with status=invited.
    assert roster[c_email]["role"] == "viewer" and roster[c_email]["status"] == "invited"
    assert d_email not in roster

    # Active editor can read the roster; the invited user and an outsider cannot.
    assert (
        client.get(f"/v1/workspaces/{workspace}/members", headers=_auth(b_token)).status_code
        == 200
    )
    invited = client.get(f"/v1/workspaces/{workspace}/members", headers=_auth(c_token))
    assert invited.status_code == 403
    assert invited.json()["detail"]["error"] == "not_a_member"
    outsider = client.get(f"/v1/workspaces/{workspace}/members", headers=_auth(d_token))
    assert outsider.status_code == 403
    assert outsider.json()["detail"]["error"] == "not_a_member"


def test_my_invitations_are_caller_filtered(client) -> None:
    _a_email, a_token, _ = _new_user(client)
    b_email, b_token, _ = _new_user(client)
    c_email, c_token, _ = _new_user(client)
    workspace = _create_workspace(client, a_token)
    _invite(client, a_token, workspace, b_email, "editor")
    _invite(client, a_token, workspace, c_email, "viewer")
    assert (
        client.post(
            f"/v1/workspaces/{workspace}/members/accept", headers=_auth(b_token)
        ).status_code
        == 200
    )

    # C has one pending invitation; B accepted so has none; A (owner) has none.
    c_invites = client.get("/v1/me/invitations", headers=_auth(c_token)).json()
    assert len(c_invites) == 1
    assert set(c_invites[0]) == INVITATION_FIELDS
    assert c_invites[0]["workspace_id"] == workspace
    assert c_invites[0]["workspace_name"] == "Roster"
    assert c_invites[0]["role"] == "viewer"

    assert client.get("/v1/me/invitations", headers=_auth(b_token)).json() == []
    assert client.get("/v1/me/invitations", headers=_auth(a_token)).json() == []


def test_members_invalid_uuid_uses_lean_422(client) -> None:
    _email, token, _ = _new_user(client)
    response = client.get("/v1/workspaces/not-a-uuid/members", headers=_auth(token))
    assert response.status_code == 422
    assert "input" not in response.text


def test_members_requires_authentication(client) -> None:
    assert client.get(f"/v1/workspaces/{uuid.uuid4()}/members").status_code == 401
    assert client.get("/v1/me/invitations").status_code == 401
