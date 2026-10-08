"""Integration: POST /ask route (TASK-M4-002).

The route is thin, so these tests pin its contract with a fake engine injected
via ``build_answer_engine`` (no live services): auth (401/403), the 200 shape,
and the carried M4-001 mapping of ``LLMProviderError`` -> 503.
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


def _auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _new_user(client) -> str:
    email = f"m4002-{uuid.uuid4().hex}@example.test"
    registered = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201, registered.text
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _create_workspace(client, token: str) -> str:
    response = client.post("/v1/workspaces", json={"name": "Ask"}, headers=_auth(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


class _FakeEngine:
    model = "fake-answer"

    def __init__(self, answer):
        self.answer_obj = answer
        self.queries = []

    async def answer(self, *, query, acl_scope):
        self.queries.append((query, acl_scope))
        return self.answer_obj


def _sample_answer():
    from answer.models import Answer, Citation, Claim

    return Answer(
        answer="Permissions are enforced.",
        claims=[Claim(text="ACL is enforced", chunk_ids=["c-1"])],
        citations=[Citation(chunk_id="c-1", document_id="doc-a", page=1, section="Intro")],
    )


def test_ask_returns_grounded_answer(client, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.ask.router as router

    engine = _FakeEngine(_sample_answer())
    monkeypatch.setattr(router, "build_answer_engine", lambda: engine)

    token = _new_user(client)
    workspace = _create_workspace(client, token)
    response = client.post(
        f"/v1/workspaces/{workspace}/ask",
        json={"query": "what governs access?"},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"] == "Permissions are enforced."
    assert body["claims"][0]["chunk_ids"] == ["c-1"]
    assert body["citations"][0]["chunk_id"] == "c-1"
    assert body["citations"][0]["page"] == 1
    # the engine was asked within the caller's workspace scope
    assert engine.queries[0][1].workspace_ids == [workspace]


def test_ask_requires_authentication(client) -> None:
    response = client.post(f"/v1/workspaces/{uuid.uuid4()}/ask", json={"query": "q"})
    assert response.status_code == 401


def test_ask_forbidden_for_non_member(client, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.ask.router as router

    monkeypatch.setattr(router, "build_answer_engine", lambda: _FakeEngine(_sample_answer()))
    owner = _new_user(client)
    outsider = _new_user(client)
    workspace = _create_workspace(client, owner)
    response = client.post(
        f"/v1/workspaces/{workspace}/ask",
        json={"query": "q"},
        headers=_auth(outsider),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "not_a_member"


def test_ask_maps_llm_error_to_503(client, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.ask.router as router
    from models.adapters.litellm_provider import LLMProviderError

    class _DownEngine:
        model = "down"

        async def answer(self, *, query, acl_scope):
            raise LLMProviderError("gateway down")

    monkeypatch.setattr(router, "build_answer_engine", lambda: _DownEngine())
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    response = client.post(
        f"/v1/workspaces/{workspace}/ask",
        json={"query": "q"},
        headers=_auth(token),
    )
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"] == "llm_unavailable"
