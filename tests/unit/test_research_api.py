"""Research run + report API — topic in, auditable report out (T-V3-022).

Stages gated identically to T-021 via same get_principal + require + FREE-model-only.
Report returns FULL honesty package (grounded sentences with citations,
named unknowns, contradiction sides, abstention with disclosure). Prove
passthrough with staged run.

Live-server tests self-skip loudly via require_service.
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
WS_OTHER = str(uuid.uuid4())


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


def _client(monkeypatch=None, pipeline_factory=None):
    from app.authz.principal import get_principal
    from app.main import create_app
    from fastapi.testclient import TestClient

    if pipeline_factory is not None and monkeypatch is not None:
        monkeypatch.setattr("app.agents.research.create_research_pipeline", pipeline_factory)
    app = create_app()
    app.dependency_overrides[get_principal] = lambda: _principal()
    return TestClient(app)


def test_research_gate_bypass_cross_workspace_blocked(monkeypatch) -> None:
    """Gate bypasses via research routes: 0 — cross-workspace BLOCKED."""
    client = _client(monkeypatch)
    # Cross-workspace submit
    resp = client.post(f"/v1/workspaces/{WS_OTHER}/agent/research", json={"topic": "test topic"})
    assert resp.status_code == 403, resp.text
    # Create in correct workspace then try to fetch via other workspace
    ok = client.post(f"/v1/workspaces/{WS_ID}/agent/research", json={"topic": "test topic"})
    assert ok.status_code == 201, ok.text
    rid = ok.json()["research_id"]
    cross = client.get(f"/v1/workspaces/{WS_OTHER}/agent/research/{rid}")
    assert cross.status_code == 403
    cross2 = client.get(f"/v1/workspaces/{WS_OTHER}/agent/research/{rid}/report")
    assert cross2.status_code == 403


def test_research_unauthenticated_refused(monkeypatch) -> None:
    """Unauthenticated access: refused all (401/403 never 200)."""
    from app.authz.principal import get_principal
    from app.main import create_app
    from fastapi import HTTPException, status
    from fastapi.testclient import TestClient

    def _deny() -> None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "invalid_token", "message": "bad"})  # noqa: E501

    app = create_app()
    app.dependency_overrides[get_principal] = _deny
    client = TestClient(app)
    for url, method in [
        (f"/v1/workspaces/{WS_ID}/agent/research", "post"),
        (f"/v1/workspaces/{WS_ID}/agent/research/rid", "get"),
        (f"/v1/workspaces/{WS_ID}/agent/research/rid/report", "get"),
    ]:
        if method == "post":
            resp = client.post(url, json={"topic": "test"})
        else:
            resp = client.get(url)
        assert resp.status_code in (401, 403), f"{method} {url} expected 401/403 got {resp.status_code}"  # noqa: E501
        assert resp.status_code != 200
    # No-header variant
    app2 = create_app()
    client2 = TestClient(app2)
    resp2 = client2.post(f"/v1/workspaces/{WS_ID}/agent/research", json={"topic": "test"})
    assert resp2.status_code != 200


def test_research_report_passthrough_unknowns_contradictions_citations(monkeypatch) -> None:
    """Report content lost vs in-process: 0 - unknowns/conflicts/citations passthrough.

    T-R02: this test used to rely on the PRODUCTION skeleton manufacturing
    unknowns/contradictions whenever the topic contained the words "unknown" or
    "conflict", which is exactly the fabrication that ticket removed. The
    passthrough property is still worth proving, so it is now staged through an
    EXPLICIT test-only pipeline - the honest way to feed a report in.
    """
    topic = "test unknown and conflict topic"
    staged_unknowns = [
        {"question": topic, "searched": ["chk-x"], "reason": "NO_EVIDENCE",
         "tried_chunk_ids": []}
    ]
    staged_contradictions = [
        {"question": topic, "claim_a": "A", "claim_b": "B", "evidence_a_id": "a",
         "evidence_b_id": "b", "chunk_ids_a": ["chk-a"], "chunk_ids_b": ["chk-b"]}
    ]
    staged_outline = [f"Contradiction on '{topic}': 'A' [chk-a] vs 'B' [chk-b]"]

    async def _pipeline(_topic, _workspace_id, _principal_id):
        return {
            "grounded_sentences": [],
            "unknowns": staged_unknowns,
            "contradictions": staged_contradictions,
            "contradiction_outline": staged_outline,
            "citations": ["chk-a"],
            "abstained": True,
            "abstention_reason": "partial: no groundable content",
            "searched": ["chk-x"],
            "limitations": [],
        }

    monkeypatch.setattr(
        "app.agents.research.create_research_pipeline", lambda: _pipeline
    )
    client = _client(monkeypatch)
    resp = client.post(f"/v1/workspaces/{WS_ID}/agent/research", json={"topic": topic})
    assert resp.status_code == 201, resp.text
    rid = resp.json()["research_id"]

    # Status
    st = client.get(f"/v1/workspaces/{WS_ID}/agent/research/{rid}")
    assert st.status_code == 200
    assert st.json()["topic"] == topic

    # Report - must carry unknowns + contradictions + citations verbatim
    rpt = client.get(f"/v1/workspaces/{WS_ID}/agent/research/{rid}/report")
    assert rpt.status_code == 200, rpt.text
    body = rpt.json()
    assert body["topic"] == topic
    # Unknowns passthrough
    assert any(u["question"] == topic for u in body["unknowns"]) or len(body["unknowns"]) >= 1
    # Contradictions passthrough
    assert len(body["contradictions"]) >= 1
    assert len(body["contradiction_outline"]) >= 1
    assert "chk-a" in body["contradiction_outline"][0] and "chk-b" in body["contradiction_outline"][0]  # noqa: E501
    # A staged pipeline is a configured path, so it is NOT reported unavailable
    assert body["error_code"] is None
    # Ensure at least unknowns or grounded present, not lost
    assert body["unknowns"] or body["grounded_sentences"] or body["abstained"]


def test_research_abstention_discloses_searched(monkeypatch) -> None:
    """Abstention honesty — zero groundable still discloses what was searched."""
    # Directly test staged abstention via topic that triggers no grounded path
    # Use pipeline patch to force abstained report
    async def _abstain_pipeline(topic, workspace_id, principal_id):
        return {
            "grounded_sentences": [],
            "unknowns": [{"question": topic, "searched": [topic], "reason": "NO_EVIDENCE", "tried_chunk_ids": []}],  # noqa: E501
            "contradictions": [],
            "contradiction_outline": [],
            "citations": [],
            "abstained": True,
            "abstention_reason": "No groundable content: all claims failed grounding",
            "searched": [topic],
            "limitations": [f"Searched: {topic}"],
        }

    client = _client(monkeypatch, lambda: _abstain_pipeline)
    resp = client.post(f"/v1/workspaces/{WS_ID}/agent/research", json={"topic": "abstention test"})
    assert resp.status_code == 201
    rid = resp.json()["research_id"]
    rpt = client.get(f"/v1/workspaces/{WS_ID}/agent/research/{rid}/report").json()
    assert rpt["abstained"] is True
    assert rpt["abstention_reason"]
    assert rpt["searched"] == ["abstention test"]
    assert any("Searched" in lim for lim in rpt["limitations"])


def test_research_inprocess_parity_via_staged_pipeline(monkeypatch) -> None:
    """Report content vs in-process staged pipeline — no loss."""
    # In-process staged pipeline (same fakes as HTTP)
    async def _staged_pipeline(topic, workspace_id, principal_id):
        return {
            "grounded_sentences": [{"sentence": "The budget is 900 dollars.", "chunk_ids": ["chk-1"], "evidence_ids": ["ev-1"], "citations": ["chk-1"]}],  # noqa: E501
            "unknowns": [{"question": "What is scope?", "searched": ["chk-x"], "reason": "NO_EVIDENCE", "tried_chunk_ids": []}],  # noqa: E501
            "contradictions": [{"question": "What is budget?", "claim_a": "900", "claim_b": "1900", "evidence_a_id": "a", "evidence_b_id": "b", "chunk_ids_a": ["chk-a"], "chunk_ids_b": ["chk-b"]}],  # noqa: E501
            "contradiction_outline": ["Contradiction on 'What is budget?': '900' [chk-a] vs '1900' [chk-b]"],  # noqa: E501
            "citations": ["chk-1"],
            "abstained": False,
            "abstention_reason": "",
            "searched": [topic],
            "limitations": [],
        }

    client = _client(monkeypatch, lambda: _staged_pipeline)
    topic = "parity test"
    resp = client.post(f"/v1/workspaces/{WS_ID}/agent/research", json={"topic": topic})
    assert resp.status_code == 201
    rid = resp.json()["research_id"]
    http_report = client.get(f"/v1/workspaces/{WS_ID}/agent/research/{rid}/report").json()

    import asyncio

    staged = asyncio.run(_staged_pipeline(topic, WS_ID, "user-1"))
    assert http_report["grounded_sentences"] == staged["grounded_sentences"]
    assert http_report["unknowns"] == staged["unknowns"]
    assert http_report["contradictions"] == staged["contradictions"]
    assert http_report["citations"] == staged["citations"]


def test_live_research_require_service(api_client, workspaces) -> None:
    """Live: research route mounted and auth-enforced; skipped loudly when stack down."""
    ws_id = workspaces.a.id
    resp = api_client.get(f"/v1/workspaces/{ws_id}/agent/research/nonexistent")
    assert resp.status_code in (401, 403, 404), resp.text
    assert resp.status_code != 200
    resp2 = api_client.post(f"/v1/workspaces/{ws_id}/agent/research", json={"topic": "live"})
    assert resp2.status_code in (401, 403, 422), resp2.text
    assert resp2.status_code != 200
