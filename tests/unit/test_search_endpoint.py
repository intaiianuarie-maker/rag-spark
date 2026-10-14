"""Unit tests: POST /v1/workspaces/{id}/search (TASK-M5-002a).

Thin-endpoint contract over the frozen M3-004 orchestrator, all on fakes
(no services): hybrid default, per-mode ranked results, ACL scoping via the
``get_principal`` Depends, and typed filter mapping into ``SearchFilter``
(pre-ranking) + date narrowing (post-retrieval).
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.unit

WS_ID = str(uuid.uuid4())


def _principal(user_id: str = "user-1", workspaces: dict | None = None):
    from core.auth import Principal, Role

    if workspaces is None:
        workspaces = {WS_ID: Role.EDITOR}
    return Principal(
        user_id=user_id,
        email=f"{user_id}@example.test",
        is_authenticated=True,
        workspace_roles=dict(workspaces),
    )


class _FakeOrchestrator:
    def __init__(self, items: list) -> None:
        self._items = list(items)
        self.calls: list[dict] = []

    async def retrieve(self, *, query, acl_scope, mode=None, **kwargs):
        self.calls.append(
            {
                "query": query,
                "acl_scope": acl_scope,
                "mode": mode,
                "extra": dict(kwargs),
            }
        )
        return list(self._items)


def _item(**over):
    from retrieval.interfaces import RetrievalMethod, RetrievedItem

    base: dict = {
        "content": "evidence about permissions",
        "score": 0.42,
        "method": RetrievalMethod.FUSED,
        "workspace_id": WS_ID,
        "document_id": "doc-1",
        "chunk_id": "c-1",
        "page": 1,
        "section": "Intro",
        "scores": {"rrf": 0.03},
        "metadata": {},
    }
    base.update(over)
    return RetrievedItem(**base)


def _client(monkeypatch, orchestrator):
    import app.search.search as search_module
    from app.authz.principal import get_principal
    from app.main import create_app
    from fastapi.testclient import TestClient

    # Route calls module-level app.search.search.create_orchestrator
    # (lazy factory seam); that single target is what tests patch.
    monkeypatch.setattr(search_module, "create_orchestrator", lambda sensitive=False: orchestrator)
    app = create_app()
    app.dependency_overrides[get_principal] = lambda: _principal()
    return TestClient(app)


def test_search_hybrid_default_returns_ranked_results(monkeypatch) -> None:
    orch = _FakeOrchestrator([_item(), _item(chunk_id="c-2", score=0.1)])
    client = _client(monkeypatch, orch)
    response = client.post(f"/v1/workspaces/{WS_ID}/search", json={"query": "permissions"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "hybrid"
    assert [r["chunk_id"] for r in body["results"]] == ["c-1", "c-2"]
    assert body["results"][0]["scores"] == {"rrf": 0.03}
    call = orch.calls[0]
    assert call["acl_scope"].workspace_ids == [WS_ID]


@pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
def test_search_modes_forwarded_to_orchestrator(monkeypatch, mode: str) -> None:
    orch = _FakeOrchestrator([_item()])
    client = _client(monkeypatch, orch)
    response = client.post(f"/v1/workspaces/{WS_ID}/search", json={"query": "q", "mode": mode})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == mode
    assert str(orch.calls[0]["mode"]) == mode


def test_search_requires_authentication(monkeypatch) -> None:
    import app.search.search as search_module
    from app.authz.principal import get_principal
    from app.main import create_app
    from fastapi import HTTPException, status
    from fastapi.testclient import TestClient

    def _deny() -> None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_token", "message": "denied"},
        )

    orch = _FakeOrchestrator([_item()])
    monkeypatch.setattr(search_module, "create_orchestrator", lambda sensitive=False: orch)
    app = create_app()
    app.dependency_overrides[get_principal] = _deny
    client = TestClient(app)
    response = client.post(f"/v1/workspaces/{WS_ID}/search", json={"query": "q"})
    assert response.status_code == 401
    assert not orch.calls


def test_search_forbidden_for_non_member(monkeypatch) -> None:
    import app.search.search as search_module
    from app.authz.principal import get_principal
    from app.main import create_app
    from fastapi.testclient import TestClient

    orch = _FakeOrchestrator([_item()])
    monkeypatch.setattr(search_module, "create_orchestrator", lambda sensitive=False: orch)
    app = create_app()
    app.dependency_overrides[get_principal] = lambda: _principal(workspaces={})
    client = TestClient(app)
    response = client.post(f"/v1/workspaces/{WS_ID}/search", json={"query": "q"})
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "not_a_member"
    assert not orch.calls


def test_search_filters_map_to_search_filter(monkeypatch) -> None:
    orch = _FakeOrchestrator([_item()])
    client = _client(monkeypatch, orch)
    response = client.post(
        f"/v1/workspaces/{WS_ID}/search",
        json={
            "query": "q",
            "filters": {
                "type": "pdf",
                "source": "upload",
                "collection": "col-1",
                "language": "en",
            },
        },
    )
    assert response.status_code == 200, response.text
    extra_filter = orch.calls[0]["extra"]["extra_filter"]
    assert extra_filter.workspace_ids == [WS_ID]
    assert extra_filter.language == "en"
    assert extra_filter.metadata == {"type": "pdf", "source": "upload", "collection": "col-1"}


def test_search_date_filter_narrows(monkeypatch) -> None:
    orch = _FakeOrchestrator(
        [
            _item(chunk_id="c-1", metadata={"created_at": "2026-10-01"}),
            _item(chunk_id="c-2", metadata={"created_at": "2026-09-01"}),
        ]
    )
    client = _client(monkeypatch, orch)
    response = client.post(
        f"/v1/workspaces/{WS_ID}/search",
        json={"query": "q", "filters": {"date": "2026-10-01"}},
    )
    assert response.status_code == 200, response.text
    assert [r["chunk_id"] for r in response.json()["results"]] == ["c-1"]


def test_search_never_widens_acl_scope() -> None:
    from app.search.search import SearchFilters, build_search_filter

    filt = build_search_filter(
        workspace_id=WS_ID,
        filters=SearchFilters(type="pdf", language="en"),
    )
    assert filt.workspace_ids == [WS_ID]
    assert filt.metadata == {"type": "pdf"}
    assert filt.language == "en"


def test_search_invalid_mode_rejected(monkeypatch) -> None:
    orch = _FakeOrchestrator([_item()])
    client = _client(monkeypatch, orch)
    response = client.post(f"/v1/workspaces/{WS_ID}/search", json={"query": "q", "mode": "nope"})
    assert response.status_code == 422
    assert not orch.calls


def test_search_empty_query_rejected(monkeypatch) -> None:
    orch = _FakeOrchestrator([_item()])
    client = _client(monkeypatch, orch)
    response = client.post(f"/v1/workspaces/{WS_ID}/search", json={"query": ""})
    assert response.status_code == 422
    assert not orch.calls


def test_search_graph_and_hybrid_graph_modes_accepted(monkeypatch) -> None:
    orch = _FakeOrchestrator([_item(chunk_id="g-1", metadata={"is_graph": True})])
    client = _client(monkeypatch, orch)

    # Test GRAPH mode
    res_graph = client.post(
        f"/v1/workspaces/{WS_ID}/search",
        json={"query": "knowledge graph entity", "mode": "graph"},
    )
    assert res_graph.status_code == 200, res_graph.text
    assert res_graph.json()["mode"] == "graph"
    assert orch.calls[-1]["mode"] == "graph"

    # Test HYBRID_GRAPH mode
    res_hybrid_graph = client.post(
        f"/v1/workspaces/{WS_ID}/search",
        json={"query": "hybrid graph context", "mode": "hybrid_graph"},
    )
    assert res_hybrid_graph.status_code == 200, res_hybrid_graph.text
    assert res_hybrid_graph.json()["mode"] == "hybrid_graph"
    assert orch.calls[-1]["mode"] == "hybrid_graph"


def test_search_date_narrows_to_empty_sets_filters_truncated(monkeypatch) -> None:
    orch = _FakeOrchestrator(
        [
            _item(chunk_id="c-1", metadata={"created_at": "2026-10-01"}),
            _item(chunk_id="c-2", metadata={"created_at": "2026-09-01"}),
        ]
    )
    client = _client(monkeypatch, orch)
    response = client.post(
        f"/v1/workspaces/{WS_ID}/search",
        json={"query": "q", "filters": {"date": "2026-12-01"}},
    )
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["results"] == []
    assert body["filters_truncated"] is True


def test_search_plain_empty_does_not_set_filters_truncated(monkeypatch) -> None:
    orch = _FakeOrchestrator([])
    client = _client(monkeypatch, orch)
    response = client.post(f"/v1/workspaces/{WS_ID}/search", json={"query": "q"})
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["filters_truncated"] is False
