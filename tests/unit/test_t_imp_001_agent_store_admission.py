"""T-IMP-001 — queued agent runs require a shared durable store (fail-closed).

Review 2026-10-19 finding 4: the default deployment left ``AGENT_STORE_DB``
empty, so the API accepted queued runs into a process-local in-memory queue
and a separately deployed worker saw depth 0 (parent 1 / worker 0).

These tests pin:
- API admission is refused (503) when no durable store is configured.
- With a shared store the run is accepted and is visible to an INDEPENDENT
  worker process (cross-process consumption, different PID).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = [pytest.mark.unit]

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _principal(workspace_id: str):
    from core.auth import Principal, Role

    return Principal(
        user_id="user-1",
        email="user-1@example.test",
        is_authenticated=True,
        workspace_roles={workspace_id: Role.EDITOR},
    )


def _client(workspace_id: str):
    from app.authz.principal import get_principal
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    app.dependency_overrides[get_principal] = lambda: _principal(workspace_id)
    return TestClient(app)


def test_queued_run_admission_refused_without_durable_store(monkeypatch) -> None:
    """No shared durable store => admission is refused, not silently accepted."""
    monkeypatch.delenv("AGENT_STORE_DB", raising=False)
    monkeypatch.setenv("APP_ENV", "production")

    ws_id = str(uuid.uuid4())
    with _client(ws_id) as client:
        resp = client.post(f"/v1/workspaces/{ws_id}/agent/runs/queued", json={"goal": "x"})

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["detail"]["error"] == "agent_store_not_configured"
    assert "AGENT_STORE_DB" in body["detail"]["message"]


def test_queued_run_admitted_and_consumed_by_independent_worker(
    monkeypatch, tmp_path
) -> None:
    """A run accepted by the API is consumable by a separate worker process."""
    db = str(tmp_path / "agent.db")
    monkeypatch.setenv("AGENT_STORE_DB", db)
    monkeypatch.setenv("APP_ENV", "production")

    ws_id = str(uuid.uuid4())
    with _client(ws_id) as client:
        resp = client.post(f"/v1/workspaces/{ws_id}/agent/runs/queued", json={"goal": "x"})
        assert resp.status_code == 202, resp.text
        run_id = resp.json()["run_id"]

    # Independent process (different PID) opens the same shared store.
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import asyncio, os, sys\n"
                "from stores.agent_stores import set_agent_store_db, get_queue\n"
                "set_agent_store_db(os.environ['AGENT_STORE_DB'])\n"
                "q = get_queue()\n"
                "depth = asyncio.run(q.depth())\n"
                "claim = asyncio.run(q.claim(30.0))\n"
                "print('PID', os.getpid())\n"
                "print('DEPTH', depth)\n"
                "print('CLAIM', claim)\n"
            ),
        ],
        env={
            **os.environ,
            "AGENT_STORE_DB": db,
            "PYTHONPATH": os.pathsep.join(
                [
                    os.path.join(REPO_ROOT, "packages"),
                    os.path.join(REPO_ROOT, "apps", "api"),
                    os.path.join(REPO_ROOT, "apps", "worker"),
                ]
            ),
        },
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
    )
    assert child.returncode == 0, child.stderr
    assert "DEPTH 1" in child.stdout, child.stdout
    assert f"CLAIM {run_id}" in child.stdout, child.stdout
    assert f"PID {os.getpid()}" not in child.stdout, "worker must be a separate process"
