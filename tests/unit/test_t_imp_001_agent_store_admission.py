"""T-IMP-001 — queued agent runs require a SHARED durable store (fail-closed).

Review 2026-10-19 finding 4 + POLICE F4a/F4b:

- F4a: admission read a raw ``APP_ENV`` from ``os.environ`` instead of the
  RESOLVED settings, so a ``.env`` with ``APP_ENV=production`` logged
  production while still accepting runs into a process-local queue.
- F4b: a *non-empty* ``AGENT_STORE_DB`` did not establish shared storage: two
  containers with the same relative path have two different filesystems
  (durable SQLite queue at depth 1 versus 0).

These tests pin:
- admission is refused (503) in production/staging without a durable store;
- admission is refused (503) when the configured path is outside the shared
  root (the two-filesystem case);
- a ``.env``-resolved production environment is honoured (dotenv case);
- a run accepted through a shared path is consumable by an INDEPENDENT
  process and survives a restart;
- the rendered compose deployment mounts the shared volume into BOTH api and
  worker and points the store at it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def _reset_settings() -> None:
    from core.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
def production_env(monkeypatch):
    """A production environment with a shared durable agent store root."""
    monkeypatch.setenv("APP_ENV", "production")
    _reset_settings()
    yield
    _reset_settings()


def _shared_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "shared"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_STORE_SHARED_ROOT", str(root))
    return root


def test_queued_run_admission_refused_without_durable_store(
    monkeypatch, production_env
) -> None:
    """No durable store in production => admission refused, not silently accepted."""
    monkeypatch.delenv("AGENT_STORE_DB", raising=False)

    ws_id = str(uuid.uuid4())
    with _client(ws_id) as client:
        resp = client.post(f"/v1/workspaces/{ws_id}/agent/runs/queued", json={"goal": "x"})

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["detail"]["error"] == "agent_store_not_configured"
    assert "AGENT_STORE_DB" in body["detail"]["message"]


def test_queued_run_admission_refused_for_unshared_path(
    monkeypatch, production_env, tmp_path
) -> None:
    """F4b: a path outside the shared root is not shared storage -> refused."""
    root = _shared_root(tmp_path, monkeypatch)
    unshared = tmp_path / "elsewhere" / "agent.db"
    unshared.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_STORE_DB", str(unshared))

    ws_id = str(uuid.uuid4())
    with _client(ws_id) as client:
        resp = client.post(f"/v1/workspaces/{ws_id}/agent/runs/queued", json={"goal": "x"})

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["detail"]["error"] == "agent_store_unshared"
    # The refusal happens BEFORE anything is persisted.
    assert str(root) in body["detail"]["message"]


def test_queued_run_admission_uses_resolved_settings_from_dotenv(
    monkeypatch, tmp_path
) -> None:
    """F4a: the environment comes from the RESOLVED settings (``.env`` included).

    A temporary ``.env`` with ``APP_ENV=production`` and no ``AGENT_STORE_DB``
    process variable must refuse admission — a raw ``os.environ`` read would
    have seen no APP_ENV and accepted the run.
    """
    workdir = tmp_path / "dotenv-case"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / ".env").write_text(
        "APP_ENV=production\nAGENT_STORE_DB=\n", encoding="utf-8"
    )
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("AGENT_STORE_DB", raising=False)
    monkeypatch.chdir(workdir)
    _reset_settings()

    from core.config import get_settings

    assert get_settings().app_env == "production"

    ws_id = str(uuid.uuid4())
    with _client(ws_id) as client:
        resp = client.post(f"/v1/workspaces/{ws_id}/agent/runs/queued", json={"goal": "x"})

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["error"] == "agent_store_not_configured"
    _reset_settings()


def test_queued_run_accepted_and_consumed_by_independent_worker(
    monkeypatch, production_env, tmp_path
) -> None:
    """A run accepted through a shared path is consumable by another process."""
    root = _shared_root(tmp_path, monkeypatch)
    db = root / "agent.db"
    monkeypatch.setenv("AGENT_STORE_DB", str(db))

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
                "import asyncio, os\n"
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
            "AGENT_STORE_DB": str(db),
            "PYTHONPATH": os.pathsep.join(
                [
                    str(REPO_ROOT / "packages"),
                    str(REPO_ROOT / "apps" / "api"),
                    str(REPO_ROOT / "apps" / "worker"),
                ]
            ),
        },
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )
    assert child.returncode == 0, child.stderr
    assert "DEPTH 1" in child.stdout, child.stdout
    assert f"CLAIM {run_id}" in child.stdout, child.stdout
    assert f"PID {os.getpid()}" not in child.stdout, "worker must be a separate process"


def test_queued_run_survives_restart(
    monkeypatch, production_env, tmp_path
) -> None:
    """Restart persistence: the accepted run is still queued for a fresh process."""
    root = _shared_root(tmp_path, monkeypatch)
    db = root / "agent.db"
    monkeypatch.setenv("AGENT_STORE_DB", str(db))

    ws_id = str(uuid.uuid4())
    with _client(ws_id) as client:
        resp = client.post(f"/v1/workspaces/{ws_id}/agent/runs/queued", json={"goal": "x"})
        assert resp.status_code == 202, resp.text
        run_id = resp.json()["run_id"]

    # "Restart": a brand new process reads the same durable store.
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import asyncio, os\n"
                "from stores.agent_stores import set_agent_store_db, get_queue\n"
                "set_agent_store_db(os.environ['AGENT_STORE_DB'])\n"
                "q = get_queue()\n"
                "print('DEPTH', asyncio.run(q.depth()))\n"
                "print('CLAIM', asyncio.run(q.claim(30.0)))\n"
            ),
        ],
        env={
            **os.environ,
            "AGENT_STORE_DB": str(db),
            "PYTHONPATH": os.pathsep.join(
                [
                    str(REPO_ROOT / "packages"),
                    str(REPO_ROOT / "apps" / "api"),
                    str(REPO_ROOT / "apps" / "worker"),
                ]
            ),
        },
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )
    assert child.returncode == 0, child.stderr
    assert "DEPTH 1" in child.stdout, child.stdout
    assert f"CLAIM {run_id}" in child.stdout, child.stdout


def test_queued_run_refused_when_root_is_not_the_mounted_volume(
    monkeypatch, production_env, tmp_path
) -> None:
    """F4b: the resolved root must BE the mounted shared volume.

    A deployment that overrides the root without moving the volume puts the
    admitted database in separate container filesystems, so the readiness
    must not report the store as shared.
    """
    root = _shared_root(tmp_path, monkeypatch)
    db = root / "agent.db"
    monkeypatch.setenv("AGENT_STORE_DB", str(db))
    # The shared volume is mounted somewhere else entirely.
    monkeypatch.setenv("AGENT_STORE_SHARED_MOUNT", str(tmp_path / "mounted-elsewhere"))

    ws_id = str(uuid.uuid4())
    with _client(ws_id) as client:
        resp = client.post(f"/v1/workspaces/{ws_id}/agent/runs/queued", json={"goal": "x"})

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["error"] == "agent_store_root_not_mounted"


def test_api_and_worker_readiness_agree(monkeypatch, tmp_path) -> None:
    """The API and the worker must resolve the store readiness identically.

    If they disagree, the API accepts runs into a store the worker never
    opens (POLICE F4a/F4b). The worker cannot import the API package in its
    container image, so the two implementations are pinned against each other.
    """
    from app.agents.lifecycle_routes import (
        agent_store_readiness as api_readiness,
    )
    from worker.main import agent_store_readiness as worker_readiness

    root = tmp_path / "shared"
    root.mkdir(parents=True, exist_ok=True)
    cases = [
        ("production", "", None, "agent_store_not_configured"),
        ("production", str(root / "agent.db"), str(root), "agent_store_shared"),
        (
            "production",
            str(root / "agent.db"),
            str(tmp_path / "elsewhere"),
            "agent_store_root_not_mounted",
        ),
        (
            "production",
            str(tmp_path / "other" / "agent.db"),
            str(root),
            "agent_store_unshared",
        ),
        ("local", "", None, "agent_store_not_configured"),
        ("local", str(root / "agent.db"), str(root), "agent_store_shared"),
        (
            "local",
            str(tmp_path / "other" / "agent.db"),
            str(root),
            "agent_store_unshared_dev_only",
        ),
    ]
    for environment, path, mount, _expected in cases:
        monkeypatch.setenv("APP_ENV", environment)
        monkeypatch.setenv("AGENT_STORE_SHARED_ROOT", str(root))
        if mount:
            monkeypatch.setenv("AGENT_STORE_SHARED_MOUNT", mount)
        else:
            monkeypatch.delenv("AGENT_STORE_SHARED_MOUNT", raising=False)
        if path:
            monkeypatch.setenv("AGENT_STORE_DB", path)
        else:
            monkeypatch.delenv("AGENT_STORE_DB", raising=False)
        _reset_settings()

        api = api_readiness()
        worker = worker_readiness()
        assert (api.admit, api.shared, api.reason) == (
            worker.admit,
            worker.shared,
            worker.reason,
        ), f"readiness drift for {environment}/{path}"
        assert api.reason == _expected, (api.reason, _expected)
    _reset_settings()


def _render_compose(extra_env: dict | None = None) -> dict:
    """Render the compose deployment and return the parsed config."""
    import yaml

    env = {
        **os.environ,
        "AUTH_JWT_SECRET": "compose-test-secret-0123456789abcdef",
        "VECTOR_COLLECTION": "knowledge",
        "WORKFLOW_DRAFT_DB": "/app/.workflow/drafts.db",
        "WORKFLOW_RUNS_DB": "/app/.workflow/runs.db",
        "WORKFLOW_APPROVALS_DB": "/app/.workflow/approvals.db",
        "WORKFLOW_CHECKPOINT_DB": "/app/.workflow/checkpoints.db",
    }
    env.update(extra_env or {})
    rendered = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert rendered.returncode == 0, rendered.stderr
    return yaml.safe_load(rendered.stdout)


def _assert_agent_store_on_shared_volume(config: dict, expected_root: str) -> None:
    """The ADMITTED database must sit on the agentdata volume in BOTH services."""
    assert "agentdata" in config["volumes"], config["volumes"].keys()
    for service in ("api", "worker"):
        svc = config["services"][service]
        shared_root = svc["environment"]["AGENT_STORE_SHARED_ROOT"]
        db_path = svc["environment"]["AGENT_STORE_DB"]
        mount_target = svc["environment"].get("AGENT_STORE_SHARED_MOUNT", "")
        assert shared_root == expected_root, (service, shared_root, expected_root)
        assert db_path.startswith(shared_root + "/"), (service, db_path, shared_root)
        # The declared mount target is the same path the volume is mounted at,
        # so the application can refuse a root that is not the mounted volume.
        assert mount_target == expected_root, (service, mount_target, expected_root)

        mounts = {
            (m.get("type"), m.get("source"), m.get("target"))
            for m in svc.get("volumes", [])
        }
        # The shared volume is mounted AT the resolved shared root, and the
        # admitted database lives on it.
        assert ("volume", "agentdata", shared_root) in mounts, (service, mounts)
        agent_volume_target = next(
            target
            for (kind, source, target) in mounts
            if kind == "volume" and source == "agentdata"
        )
        assert db_path.startswith(agent_volume_target + "/"), (
            service,
            db_path,
            agent_volume_target,
        )


@pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker is not available on this host"
)
def test_compose_renders_shared_agent_store_volume() -> None:
    """F4b: the rendered deployment shares ONE agent volume between api and worker."""
    config = _render_compose()
    _assert_agent_store_on_shared_volume(config, "/app/.agent")


@pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker is not available on this host"
)
def test_compose_renders_custom_shared_root_volume() -> None:
    """F4b: a custom shared root must move the MOUNT, not just the path.

    A configured root that is not where the volume is mounted would put the
    admitted database in separate container filesystems (the reviewer's
    reproduction: config succeeds, api/worker readiness says shared, but the
    agentdata volume is mounted elsewhere).
    """
    custom_root = "/tmp/review-private"
    config = _render_compose({"AGENT_STORE_SHARED_ROOT": custom_root})
    _assert_agent_store_on_shared_volume(config, custom_root)
