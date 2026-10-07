"""M1-001e: invite/accept lock parity.

Runs demote-self interleaved with invite-as-owner and accept-as-owner against the
live API; the workspace row lock must keep the owner invariant (never ownerless)
and return typed statuses (no unhandled 500).
"""

from __future__ import annotations

import os
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.integration

PASSWORD = "Correct-Horse-Battery-9"
BASE = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000")


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def api() -> str:
    if not (_port_open(5432) and _port_open(6379)):
        pytest.skip("postgres/redis not reachable; run `make up`")
    try:
        health = httpx.get(f"{BASE}/health", timeout=3.0)
    except httpx.HTTPError as exc:
        pytest.skip(f"API not reachable at {BASE}: {exc}; run `make up`")
    if health.status_code != 200:
        pytest.skip(f"API at {BASE} unhealthy ({health.status_code}); run `make up`")

    from app.db import run_migrations

    run_migrations()
    return BASE


def _auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _new_user(client: httpx.Client) -> tuple[str, str, str]:
    email = f"m1e-{uuid.uuid4().hex}@example.test"
    registered = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201, registered.text
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return email, login.json()["access_token"], registered.json()["id"]


def _owner_count(workspace_id: str) -> int:
    import asyncio

    from core.config import get_settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def run() -> int:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT count(*) FROM memberships "
                        "WHERE workspace_id = :ws AND role = 'owner' AND status = 'active'"
                    ),
                    {"ws": workspace_id},
                )
                return int(result.scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _create_workspace(client: httpx.Client, token: str, name: str) -> str:
    response = client.post("/v1/workspaces", json={"name": name}, headers=_auth(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _demote_self(client: httpx.Client, token: str, workspace: str, user_id: str) -> int:
    return client.patch(
        f"/v1/workspaces/{workspace}/members/{user_id}",
        json={"role": "admin"},
        headers=_auth(token),
    ).status_code


def test_demote_self_interleaved_with_invite_as_owner(api: str) -> None:
    client = httpx.Client(base_url=api, timeout=20.0)
    for iteration in range(3):
        _a_email, a_token, a_id = _new_user(client)
        c_email, _c_token, _c_id = _new_user(client)
        workspace = _create_workspace(client, a_token, f"InviteRace {iteration}")

        def invite(
            workspace_id: str = workspace,
            email: str = c_email,
            token: str = a_token,
        ) -> int:
            return client.post(
                f"/v1/workspaces/{workspace_id}/members",
                json={"email": email, "role": "owner"},
                headers=_auth(token),
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            demote = pool.submit(_demote_self, client, a_token, workspace, a_id)
            invited = pool.submit(invite)
            demote_status, invite_status = demote.result(), invited.result()

        assert demote_status in {200, 409}, f"iter {iteration}: {demote_status}"
        assert invite_status == 201, f"iter {iteration}: {invite_status}"
        assert _owner_count(workspace) >= 1, f"iter {iteration}: ownerless workspace"


def test_demote_self_interleaved_with_accept_as_owner(api: str) -> None:
    client = httpx.Client(base_url=api, timeout=20.0)
    for iteration in range(3):
        _a_email, a_token, a_id = _new_user(client)
        b_email, b_token, _b_id = _new_user(client)
        workspace = _create_workspace(client, a_token, f"AcceptRace {iteration}")
        assert (
            client.post(
                f"/v1/workspaces/{workspace}/members",
                json={"email": b_email, "role": "owner"},
                headers=_auth(a_token),
            ).status_code
            == 201
        )

        def accept(workspace_id: str = workspace, token: str = b_token) -> int:
            return client.post(
                f"/v1/workspaces/{workspace_id}/members/accept", headers=_auth(token)
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            demote = pool.submit(_demote_self, client, a_token, workspace, a_id)
            accepted = pool.submit(accept)
            demote_status, accept_status = demote.result(), accepted.result()

        assert demote_status in {200, 409}, f"iter {iteration}: {demote_status}"
        assert accept_status == 200, f"iter {iteration}: {accept_status}"
        assert _owner_count(workspace) >= 1, f"iter {iteration}: ownerless workspace"
