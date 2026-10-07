"""M1-001d: the last-owner demote/remove race must never leave zero owners.

Drives the *running* API with two genuinely concurrent requests (the compose
stack's async server + Postgres row lock), then asserts the DB invariant
directly. Skips loudly when postgres/redis/API are unavailable.
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
    email = f"m1d-{uuid.uuid4().hex}@example.test"
    register = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert register.status_code == 201, register.text
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return email, login.json()["access_token"], register.json()["id"]


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


def test_concurrent_demote_self_never_leaves_workspace_ownerless(api: str) -> None:
    client = httpx.Client(base_url=api, timeout=20.0)
    _a_email, a_token, a_id = _new_user(client)
    b_email, b_token, b_id = _new_user(client)

    for iteration in range(3):
        workspace = client.post(
            "/v1/workspaces", json={"name": f"Race {iteration}"}, headers=_auth(a_token)
        ).json()["id"]

        invited = client.post(
            f"/v1/workspaces/{workspace}/members",
            json={"email": b_email, "role": "owner"},
            headers=_auth(a_token),
        )
        assert invited.status_code == 201, invited.text
        assert client.post(
            f"/v1/workspaces/{workspace}/members/accept", headers=_auth(b_token)
        ).status_code == 200
        assert _owner_count(workspace) == 2

        def demote_self(token: str, user_id: str, workspace_id: str = workspace) -> int:
            response = client.patch(
                f"/v1/workspaces/{workspace_id}/members/{user_id}",
                json={"role": "admin"},
                headers=_auth(token),
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(demote_self, a_token, a_id)
            second = pool.submit(demote_self, b_token, b_id)
            statuses = sorted([first.result(), second.result()])

        # Exactly one succeeds; the loser is the typed last-owner 409.
        assert statuses == [200, 409], f"iteration {iteration}: {statuses}"
        # Invariant: the workspace still has exactly one owner.
        assert _owner_count(workspace) == 1, f"iteration {iteration}: ownerless workspace"
