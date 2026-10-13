"""Security E2E placeholders — xfail(strict=False) until the owning tasks land.

TASK-M0-004 requirement 4: workspace-isolation, deleted-document and
duplicate-sync checks must exist now and be turned green *without rewriting the
harness*. Each test asserts the real enforced behaviour, fails today only
because the endpoints do not exist yet, and is marked `xfail(strict=False)` so
M1/M2/M3 implementations flip it to XPASS and then to a normal pass by removing
the marker in their own task.

`strict=False` is deliberate: a passing implementation must never break the
suite, and a still-missing implementation must never look like a green test.

V1-Q3: the deleted-document test below is now a real lifecycle test (no xfail):
upload -> index -> delete -> assert tombstone + zero search/ask retrieval.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid

import pytest

# T-V1-071: run the REAL ingest pipeline, but never into the live
# `knowledge` collection (T-V1-069). Export before any app import — pydantic
# settings cache on first read, so this must happen at import time.
from vector_collection_env import (
    assert_not_live,
    use_scratch_collection,
)

VECTOR_COLLECTION = use_scratch_collection("security")
# ...and keep the compose worker off this suite's jobs entirely, so the only
# process that runs them is this one (which points at the scratch name).
os.environ.setdefault("INGEST_QUEUE_PREFIX", "t071-security-:")
assert_not_live(VECTOR_COLLECTION)

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
    email = f"q3sec-{uuid.uuid4().hex}@example.test"
    registered = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201, registered.text
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _create_workspace(client, token: str) -> str:
    response = client.post("/v1/workspaces", json={"name": "Q3Sec"}, headers=_auth(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _client():
    from app.main import create_app
    from fastapi.testclient import TestClient

    return TestClient(create_app())


@pytest.mark.xfail(
    strict=False,
    reason="R-004 cross-workspace isolation: needs M1-001c can() + M3-001b /v1/search "
    "(workspace A secret phrase must return zero results for workspace B)",
)
def test_workspace_b_cannot_search_workspace_a_secret(workspaces) -> None:
    """Critical Security E2E from docs/engineering/testing-strategy.md."""
    with _client() as client:
        response = client.post(
            "/v1/search",
            json={"query": workspaces.a.secret_phrase, "workspace_id": workspaces.b.id},
            headers={"x-user-id": workspaces.b.principal.user_id},
        )
    assert response.status_code == 200, (
        f"search endpoint not implemented yet: {response.status_code}"
    )
    results = response.json().get("results", [])
    assert results == [], (
        f"R-004 cross-workspace leak: {workspaces.b.id} saw "
        f"{len(results)} result(s) for another workspace's secret"
    )


def test_deleted_document_is_tombstoned_and_never_retrieved(client) -> None:
    """V1-Q3 deletion lifecycle: tombstone + zero search/ask retrieval.

    Uploads uniquely-phrased content, indexes it, deletes the document, then
    asserts: detail read is an honest 404 (tombstone, never served as live),
    keyword search returns zero hits for the deleted document, and the ask
    endpoint never cites or grounds against it (verified with a stubbed answer
    engine over the real post-delete retrieval state).
    """
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    marker = f"vaulted lantern {uuid.uuid4().hex} xylophone quasar"

    uploaded = client.post(
        f"/v1/workspaces/{workspace}/documents",
        files={"file": ("vault.txt", marker.encode(), "text/plain")},
        headers=_auth(token),
    )
    assert uploaded.status_code in {200, 201, 202}, uploaded.text
    uploaded_body = uploaded.json()
    document_id = uploaded_body["document_id"]

    from jobs.ingest_worker import process_one

    asyncio.run(process_one(uuid.UUID(uploaded_body["job_id"])))

    pre = client.post(
        f"/v1/workspaces/{workspace}/search",
        json={"query": marker, "mode": "keyword"},
        headers=_auth(token),
    )
    assert pre.status_code == 200, pre.text
    assert document_id in [item["document_id"] for item in pre.json()["results"]]

    deleted = client.delete(
        f"/v1/workspaces/{workspace}/documents/{document_id}", headers=_auth(token)
    )
    assert deleted.status_code == 204, deleted.text

    detail = client.get(
        f"/v1/workspaces/{workspace}/documents/{document_id}", headers=_auth(token)
    )
    assert detail.status_code == 404
    assert detail.json()["detail"]["error"] == "document_not_found"

    search = client.post(
        f"/v1/workspaces/{workspace}/search",
        json={"query": marker, "mode": "keyword"},
        headers=_auth(token),
    )
    assert search.status_code == 200, search.text
    returned_ids = [item.get("document_id") for item in search.json().get("results", [])]
    assert document_id not in returned_ids, (
        f"deleted document still retrievable: {returned_ids}"
    )
    assert returned_ids == []

    # Ask must never cite the deleted document: drive the real post-delete
    # retrieval state into a stubbed LLM whose output claims a chunk id that
    # no longer exists, and assert verification strips it (zero citations
    # from the deleted document).
    import pytest as _pytest
    from answer.engine import AnswerEngine
    from app.ask import router as ask_router

    class _StubLLM:
        async def generate(self, request):  # type: ignore[no-untyped-def]
            import json as _json

            payload = _json.dumps(
                {
                    "answer": "The vault holds the lantern.",
                    "claims": [
                        {"text": "The vault holds the lantern.", "chunk_ids": ["dead-chunk"]}
                    ],
                }
            )
            return type("Gen", (), {"text": payload, "model": "stub", "usage": {}})()

    class _StubOrchestrator:
        async def retrieve(self, **kwargs):  # type: ignore[no-untyped-def]
            from answer.factory import create_orchestrator

            real = create_orchestrator()
            try:
                return await real.retrieve(**kwargs)
            finally:
                close = getattr(getattr(real, "dense", None), "close", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:
                        pass

    engine = AnswerEngine(
        orchestrator=_StubOrchestrator(),  # type: ignore[arg-type]
        llm=_StubLLM(),  # type: ignore[arg-type]
        model="stub",
    )

    monkeypatch = _pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(ask_router, "build_answer_engine", lambda **_: engine)
        ask = client.post(
            f"/v1/workspaces/{workspace}/ask",
            json={"query": marker},
            headers=_auth(token),
        )
    finally:
        monkeypatch.undo()
    assert ask.status_code == 200, ask.text
    from answer.models import Answer as _Answer

    ask_body = _Answer.model_validate(ask.json())
    cited_docs = [c.document_id for c in ask_body.citations]
    assert document_id not in cited_docs, f"ask cited deleted document: {cited_docs}"


@pytest.mark.xfail(
    strict=False,
    reason="duplicate sync: M2-001d dedup + M6-001b incremental sync — syncing twice "
    "must not create duplicate documents",
)
def test_duplicate_sync_does_not_duplicate_documents(workspaces) -> None:
    connector_id = "conn-local-fixture"
    with _client() as client:
        first = client.post(
            f"/v1/connectors/{connector_id}/sync",
            json={"workspace_id": workspaces.a.id},
            headers={"x-user-id": workspaces.a.principal.user_id},
        )
        second = client.post(
            f"/v1/connectors/{connector_id}/sync",
            json={"workspace_id": workspaces.a.id},
            headers={"x-user-id": workspaces.a.principal.user_id},
        )
        listing = client.get(
            "/v1/documents", params={"workspace_id": workspaces.a.id}
        )
    assert first.status_code == 200, f"connector sync not implemented yet: {first.status_code}"
    assert second.status_code == 200, second.status_code
    assert listing.status_code == 200, listing.status_code
    document_ids = [item["id"] for item in listing.json().get("items", [])]
    assert len(document_ids) == len(set(document_ids)), (
        f"duplicate documents after re-sync: {sorted(document_ids)}"
    )
