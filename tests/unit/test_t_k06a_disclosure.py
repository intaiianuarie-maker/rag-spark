"""T-K06a disclosure tests."""

from __future__ import annotations

from pathlib import Path


def _configure(monkeypatch, persistent: bool) -> None:
    if persistent:
        base = Path("/tmp/opencode/tk06a")
        base.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("WORKFLOW_DRAFT_DB", str(base / "drafts.db"))
        monkeypatch.setenv("WORKFLOW_RUNS_DB", str(base / "runs.db"))
        monkeypatch.setenv("WORKFLOW_APPROVALS_DB", str(base / "approvals.db"))
        monkeypatch.setenv("WORKFLOW_CHECKPOINT_DB", str(base / "ck.db"))
    else:
        vars = ("WORKFLOW_DRAFT_DB", "WORKFLOW_RUNS_DB",
                  "WORKFLOW_APPROVALS_DB", "WORKFLOW_CHECKPOINT_DB")
        for var in vars:
            monkeypatch.delenv(var, raising=False)


def test_disclosure_inmemory(monkeypatch) -> None:
    _configure(monkeypatch, False)
    from app.main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    resp = client.get("/ready")
    data = resp.json()
    assert data["store_modes"]["drafts"] == "in-memory"
    assert data["draft_migration"] == "pending"


def test_disclosure_persistent(monkeypatch) -> None:
    _configure(monkeypatch, True)
    from app.main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    resp = client.get("/ready")
    data = resp.json()
    assert data["store_modes"]["drafts"] == "persistent"
    assert data["draft_migration"] == "applied"
