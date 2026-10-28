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


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX absolute paths and mount tables")
def test_queued_run_refused_when_root_shadows_a_bind_mount(
    monkeypatch, production_env, tmp_path
) -> None:
    """F4b (P2): a root that covers another mount is NOT shared storage.

    POLICE's reproduction: ``AGENT_STORE_SHARED_ROOT=/app`` with
    ``AGENT_STORE_DB=/app/apps/api/review-agent.db``. The api service
    bind-mounts its source over ``/app/apps/api``, so the agent volume is
    only present where nothing else is bound: the store file lands in the
    api's own filesystem and the worker (depth 0) never sees it. The
    readiness must refuse the configuration instead of reporting it shared.
    """
    config = _render_compose(
        {
            "APP_ENV": "production",
            "AGENT_STORE_SHARED_ROOT": "/app",
            "AGENT_STORE_DB": "/app/apps/api/review-agent.db",
        }
    )
    for service in ("api", "worker"):
        env = config["services"][service]["environment"]
        for key in (
            "APP_ENV",
            "AGENT_STORE_SHARED_ROOT",
            "AGENT_STORE_SHARED_MOUNT",
            "AGENT_STORE_SHADOW_MOUNTS",
            "AGENT_STORE_DB",
        ):
            # The optional keys may be absent on an older deployment: the
            # test must fail on the BEHAVIOURAL assertion, never on setup.
            value = env.get(key)
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        # Mount evidence: the declared shared mount exists AND a source bind
        # mount shadows a subtree of it.
        monkeypatch.setenv(
            "AGENT_STORE_MOUNTINFO",
            _write_mountinfo(tmp_path, ["/", "/app", "/app/apps/api"]),
        )
        _reset_settings()

        from app.agents.lifecycle_routes import agent_store_readiness as api_ready
        from worker.main import agent_store_readiness as worker_ready

        api = api_ready()
        worker = worker_ready()
        assert (api.admit, api.shared, api.reason) == (
            worker.admit,
            worker.shared,
            worker.reason,
        ), (service, api, worker)
        assert api.admit is False, (service, api)
        assert api.shared is False, (service, api)
        assert api.reason == "agent_store_root_shadowed", (service, api)

    ws_id = str(uuid.uuid4())
    with _client(ws_id) as client:
        resp = client.post(f"/v1/workspaces/{ws_id}/agent/runs/queued", json={"goal": "x"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["error"] == "agent_store_root_shadowed"


def _escape_mountinfo_path(value: str) -> str:
    """Escape a path the way the kernel writes mountinfo.

    Space, tab, newline and backslash are written as octal sequences
    (``\\040``, ``\\011``, ``\\012``, ``\\134``).
    """
    return (
        value.replace("\\", "\\134")
        .replace("\t", "\\011")
        .replace("\n", "\\012")
        .replace(" ", "\\040")
    )


def _write_mountinfo(tmp_path: Path, mount_points: list[str], name: str = "mountinfo") -> str:
    """Write a synthetic (kernel-escaped) mountinfo file and return its path."""
    path = tmp_path / name
    lines = []
    for index, point in enumerate(mount_points):
        escaped = _escape_mountinfo_path(point)
        # mountinfo format: id parent major:minor root mountpoint options...
        lines.append(
            f"{100 + index} 1 8:1 / {escaped} rw,relatime - ext4 /dev/vda1 rw"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_intended_shared_mount_at_the_root_is_not_shadowed(
    monkeypatch, production_env, tmp_path
) -> None:
    """The happy path must NOT be refused (POLICE preflight, item 1).

    With the agentdata volume mounted AT the shared root (the intended
    shared mount sitting at its own mountpoint), production admission and the
    worker readiness must SUCCEED. Probed with a synthetic mountinfo file -
    no container required.
    """
    root = tmp_path / "app" / ".agent"
    root.mkdir(parents=True, exist_ok=True)
    db = root / "agent.db"
    monkeypatch.setenv("AGENT_STORE_SHARED_ROOT", str(root))
    monkeypatch.setenv("AGENT_STORE_SHARED_MOUNT", str(root))
    monkeypatch.setenv("AGENT_STORE_DB", str(db))
    # The ONLY mount is the intended shared volume at the root.
    monkeypatch.setenv(
        "AGENT_STORE_MOUNTINFO", _write_mountinfo(tmp_path, [str(root)])
    )
    _reset_settings()

    from app.agents.lifecycle_routes import agent_store_readiness as api_ready
    from worker.main import agent_store_readiness as worker_ready

    api = api_ready()
    worker = worker_ready()
    assert (api.admit, api.shared) == (True, True), api
    assert (worker.admit, worker.shared) == (True, True), worker

    ws_id = str(uuid.uuid4())
    with _client(ws_id) as client:
        resp = client.post(f"/v1/workspaces/{ws_id}/agent/runs/queued", json={"goal": "x"})
    assert resp.status_code == 202, resp.text


def test_nested_mount_inside_the_root_shadows_the_store(
    monkeypatch, production_env, tmp_path
) -> None:
    """A mount INSIDE the root is a competing mount -> the store is refused."""
    root = tmp_path / "app"
    root.mkdir(parents=True, exist_ok=True)
    nested = root / "apps" / "api"
    nested.mkdir(parents=True, exist_ok=True)
    db = nested / "review-agent.db"
    monkeypatch.setenv("AGENT_STORE_SHARED_ROOT", str(root))
    monkeypatch.setenv("AGENT_STORE_SHARED_MOUNT", str(root))
    monkeypatch.setenv("AGENT_STORE_DB", str(db))
    # The intended volume is at the root AND a source bind mount shadows a
    # subtree of it.
    monkeypatch.setenv(
        "AGENT_STORE_MOUNTINFO",
        _write_mountinfo(tmp_path, [str(root), str(nested)]),
    )
    _reset_settings()

    from app.agents.lifecycle_routes import agent_store_readiness as api_ready
    from worker.main import agent_store_readiness as worker_ready

    api = api_ready()
    worker = worker_ready()
    assert (api.admit, api.shared, api.reason) == (
        worker.admit,
        worker.shared,
        worker.reason,
    ), (api, worker)
    assert api.admit is False, api
    assert api.reason == "agent_store_root_shadowed", api


def test_declared_shared_mount_without_mount_evidence_is_refused(
    monkeypatch, production_env, tmp_path
) -> None:
    """F4b (P2): a DECLARED shared mount must be proven by the mount table.

    With ``AGENT_STORE_SHARED_MOUNT`` declared, the mount table must contain
    an entry AT that mount point. A table that only shows a COMPETING
    ancestor proves nothing about the intended mount, so the store is refused
    instead of being reported shared.
    """
    root = tmp_path / "app" / "apps" / "api" / "private"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_STORE_SHARED_ROOT", str(root))
    monkeypatch.setenv("AGENT_STORE_SHARED_MOUNT", str(root))
    monkeypatch.setenv("AGENT_STORE_DB", str(root / "agent.db"))
    # Only the competing ancestor is present: no entry at the intended mount.
    monkeypatch.setenv(
        "AGENT_STORE_MOUNTINFO",
        _write_mountinfo(tmp_path, ["/", str(root.parent)]),
    )
    _reset_settings()

    from app.agents.lifecycle_routes import agent_store_readiness as api_ready
    from worker.main import agent_store_readiness as worker_ready

    api = api_ready()
    worker = worker_ready()
    assert (api.admit, api.shared, api.reason) == (
        worker.admit,
        worker.shared,
        worker.reason,
    ), (api, worker)
    assert api.admit is False, api
    assert api.shared is False, api
    assert api.reason == "agent_store_mount_unverified", api


def test_declared_shared_mount_with_unreadable_mountinfo_is_refused(
    monkeypatch, production_env, tmp_path
) -> None:
    """F4b (P2): unreadable mount evidence is a refusal, not a pass."""
    root = tmp_path / "shared"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_STORE_SHARED_ROOT", str(root))
    monkeypatch.setenv("AGENT_STORE_SHARED_MOUNT", str(root))
    monkeypatch.setenv("AGENT_STORE_DB", str(root / "agent.db"))
    # The mount table path is a DIRECTORY: it cannot be read.
    monkeypatch.setenv("AGENT_STORE_MOUNTINFO", str(tmp_path))
    _reset_settings()

    from app.agents.lifecycle_routes import agent_store_readiness as api_ready
    from worker.main import agent_store_readiness as worker_ready

    api = api_ready()
    worker = worker_ready()
    assert (api.admit, api.shared, api.reason) == (
        worker.admit,
        worker.shared,
        worker.reason,
    ), (api, worker)
    assert api.admit is False, api
    assert api.reason == "agent_store_mount_unverified", api


def test_host_mode_without_declared_shared_mount_still_admits(
    monkeypatch, production_env, tmp_path
) -> None:
    """The valid host mode (no declared shared mount) keeps admitting.

    Outside the compose deployment the api and worker processes share one host
    filesystem; a configuration that declares no shared mount must not be
    refused for lack of mount evidence.
    """
    root = tmp_path / "shared"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_STORE_SHARED_ROOT", str(root))
    monkeypatch.delenv("AGENT_STORE_SHARED_MOUNT", raising=False)
    monkeypatch.setenv("AGENT_STORE_DB", str(root / "agent.db"))
    monkeypatch.setenv(
        "AGENT_STORE_MOUNTINFO", _write_mountinfo(tmp_path, ["/", str(root)])
    )
    _reset_settings()

    from app.agents.lifecycle_routes import agent_store_readiness as api_ready
    from worker.main import agent_store_readiness as worker_ready

    api = api_ready()
    worker = worker_ready()
    assert (api.admit, api.shared) == (True, True), (api, worker)
    assert (worker.admit, worker.shared) == (True, True), (api, worker)


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX absolute paths and mount tables")
def test_nested_mount_under_escaped_root_is_detected(
    monkeypatch, production_env, tmp_path
) -> None:
    """F4b (P2): octal escapes must be decoded in ONE pass.

    A nested mount under a root whose path contains a backslash or a tab is
    written octal-escaped in the mount table (``\\134`` / ``\\011``). A
    decoder that only handles the space escape misses it and the shadowed
    store is admitted.
    """
    for label, root_name in (("backslash", "shared\\root"), ("tab", "shared\troot")):
        root = tmp_path / root_name
        root.mkdir(parents=True, exist_ok=True)
        nested = root / "private"
        nested.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("AGENT_STORE_SHARED_ROOT", str(root))
        monkeypatch.setenv("AGENT_STORE_SHARED_MOUNT", str(root))
        monkeypatch.setenv("AGENT_STORE_DB", str(nested / "agent.db"))
        # The kernel escapes the path; the nested mount sits under the root.
        monkeypatch.setenv(
            "AGENT_STORE_MOUNTINFO",
            _write_mountinfo(tmp_path, ["/", str(root), str(nested)]),
        )
        _reset_settings()

        from app.agents.lifecycle_routes import agent_store_readiness as api_ready
        from worker.main import agent_store_readiness as worker_ready

        api = api_ready()
        worker = worker_ready()
        assert (api.admit, api.shared, api.reason) == (
            worker.admit,
            worker.shared,
            worker.reason,
        ), (label, api, worker)
        assert api.admit is False, (label, api)
        assert api.reason == "agent_store_root_shadowed", (label, api)


def test_escaped_intended_mount_target_still_validates(
    monkeypatch, production_env, tmp_path
) -> None:
    """F4b (P2): a correctly escaped intended mount still validates and admits."""
    root = tmp_path / "shared root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_STORE_SHARED_ROOT", str(root))
    monkeypatch.setenv("AGENT_STORE_SHARED_MOUNT", str(root))
    monkeypatch.setenv("AGENT_STORE_DB", str(root / "agent.db"))
    # The mount table writes the root escaped as \040 (space).
    monkeypatch.setenv(
        "AGENT_STORE_MOUNTINFO", _write_mountinfo(tmp_path, ["/", str(root)])
    )
    _reset_settings()

    from app.agents.lifecycle_routes import agent_store_readiness as api_ready
    from worker.main import agent_store_readiness as worker_ready

    api = api_ready()
    worker = worker_ready()
    assert (api.admit, api.shared) == (True, True), (api, worker)
    assert (worker.admit, worker.shared) == (True, True), (api, worker)


def test_relative_store_paths_are_refused_across_working_directories(
    monkeypatch, production_env, tmp_path
) -> None:
    """F4b (P2): a RELATIVE store path is working-directory dependent.

    An api and a worker started from DIFFERENT working directories both
    reported ``admit=True/shared=True`` while resolving DIFFERENT databases:
    the api accepted a run (202, queue depth 1) and the worker saw depth 0 /
    claim None. A relative path can never be shared, so it is refused.
    """
    api_cwd = tmp_path / "api-cwd"
    worker_cwd = tmp_path / "worker-cwd"
    api_cwd.mkdir()
    worker_cwd.mkdir()
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AGENT_STORE_SHARED_ROOT", "state")
    monkeypatch.setenv("AGENT_STORE_DB", "state/agent.db")
    monkeypatch.delenv("AGENT_STORE_SHARED_MOUNT", raising=False)
    monkeypatch.delenv("AGENT_STORE_MOUNTINFO", raising=False)

    from app.agents.lifecycle_routes import agent_store_readiness as api_ready
    from worker.main import agent_store_readiness as worker_ready

    outcomes = []
    for cwd in (api_cwd, worker_cwd):
        monkeypatch.chdir(cwd)
        _reset_settings()
        api = api_ready()
        worker = worker_ready()
        outcomes.append((api, worker))
        # Neither service may claim the store is shared.
        assert api.admit is False, (cwd, api)
        assert api.shared is False, (cwd, api)
        assert worker.admit is False, (cwd, worker)
        assert worker.shared is False, (cwd, worker)
        assert api.reason == "agent_store_path_not_absolute", (cwd, api)

    # Both services must agree, and no admission may have happened.
    (api_a, worker_a), (api_b, worker_b) = outcomes
    assert api_a.reason == api_b.reason == worker_a.reason == worker_b.reason
    monkeypatch.chdir(tmp_path)

    ws_id = str(uuid.uuid4())
    with _client(ws_id) as client:
        resp = client.post(f"/v1/workspaces/{ws_id}/agent/runs/queued", json={"goal": "x"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["error"] == "agent_store_path_not_absolute"


def test_tilde_relative_store_paths_are_refused_across_working_directories(
    monkeypatch, production_env, tmp_path
) -> None:
    """F4b (P2): a TILDE-RELATIVE path is not raw-absolute -> refused.

    "~/state" (and the "~root/state" variant) is expanded at validation time
    but the raw string was used afterwards, so the store opened a literal
    "~" directory relative to the process working directory: api and worker
    with different CWDs (or different HOME) opened different databases while
    both reported the store as shared.
    """
    api_cwd = tmp_path / "api-cwd"
    worker_cwd = tmp_path / "worker-cwd"
    api_cwd.mkdir()
    worker_cwd.mkdir()

    from app.agents.lifecycle_routes import agent_store_readiness as api_ready
    from worker.main import agent_store_readiness as worker_ready

    for label, shared in (("tilde", "~/state"), ("named-user", "~root/state")):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("AGENT_STORE_SHARED_ROOT", shared)
        monkeypatch.setenv("AGENT_STORE_DB", shared + "/agent.db")
        monkeypatch.delenv("AGENT_STORE_SHARED_MOUNT", raising=False)
        monkeypatch.delenv("AGENT_STORE_MOUNTINFO", raising=False)
        for cwd in (api_cwd, worker_cwd):
            monkeypatch.chdir(cwd)
            _reset_settings()
            api = api_ready()
            worker = worker_ready()
            assert api.admit is False, (label, cwd, api)
            assert api.shared is False, (label, cwd, api)
            assert worker.admit is False, (label, cwd, worker)
            assert worker.shared is False, (label, cwd, worker)
            assert api.reason == "agent_store_path_not_absolute", (label, cwd, api)
            assert worker.reason == api.reason, (label, cwd, api, worker)
    monkeypatch.chdir(tmp_path)

    ws_id = str(uuid.uuid4())
    with _client(ws_id) as client:
        resp = client.post(f"/v1/workspaces/{ws_id}/agent/runs/queued", json={"goal": "x"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["error"] == "agent_store_path_not_absolute"


def test_absolute_path_with_a_literal_tilde_component_still_admits(
    monkeypatch, production_env, tmp_path
) -> None:
    """A raw-absolute path CONTAINING a tilde component stays valid.

    "/app/dist/~archive/state" is absolute: the tilde is an ordinary path
    character there, and the store is still shared between the processes.
    """
    shared = tmp_path / "dist" / "~archive" / "state"
    shared.mkdir(parents=True, exist_ok=True)
    db = shared / "agent.db"
    monkeypatch.setenv("AGENT_STORE_SHARED_ROOT", str(shared))
    monkeypatch.setenv("AGENT_STORE_DB", str(db))
    monkeypatch.delenv("AGENT_STORE_SHARED_MOUNT", raising=False)
    monkeypatch.delenv("AGENT_STORE_MOUNTINFO", raising=False)
    _reset_settings()

    from app.agents.lifecycle_routes import agent_store_readiness as api_ready
    from worker.main import agent_store_readiness as worker_ready

    api = api_ready()
    worker = worker_ready()
    assert (api.admit, api.shared) == (True, True), api
    assert (worker.admit, worker.shared) == (True, True), worker
    # The published path is the normalized absolute one (no expansion).
    assert api.path == str(db), api
    assert worker.path == str(db), worker


def test_absolute_store_paths_are_shared_across_working_directories(
    monkeypatch, production_env, tmp_path
) -> None:
    """The absolute host-mode guard is retained: api and worker share the store.

    The same relative-shaped configuration with ABSOLUTE paths must still be
    admitted, and an independent worker process started from a DIFFERENT
    working directory must see and claim the accepted run.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    db = shared / "agent.db"
    monkeypatch.setenv("AGENT_STORE_SHARED_ROOT", str(shared))
    monkeypatch.setenv("AGENT_STORE_DB", str(db))
    monkeypatch.delenv("AGENT_STORE_SHARED_MOUNT", raising=False)
    monkeypatch.delenv("AGENT_STORE_MOUNTINFO", raising=False)

    api_cwd = tmp_path / "api-cwd"
    worker_cwd = tmp_path / "worker-cwd"
    api_cwd.mkdir()
    worker_cwd.mkdir()

    from app.agents.lifecycle_routes import agent_store_readiness as api_ready
    from worker.main import agent_store_readiness as worker_ready

    monkeypatch.chdir(api_cwd)
    _reset_settings()
    api = api_ready()
    worker = worker_ready()
    assert (api.admit, api.shared) == (True, True), api
    assert (worker.admit, worker.shared) == (True, True), worker

    ws_id = str(uuid.uuid4())
    with _client(ws_id) as client:
        resp = client.post(f"/v1/workspaces/{ws_id}/agent/runs/queued", json={"goal": "x"})
        assert resp.status_code == 202, resp.text
        run_id = resp.json()["run_id"]

    # An independent worker process started from a DIFFERENT working
    # directory must see and claim the accepted run.
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
        cwd=str(worker_cwd),
    )
    assert child.returncode == 0, child.stderr
    assert "DEPTH 1" in child.stdout, child.stdout
    assert f"CLAIM {run_id}" in child.stdout, child.stdout
    monkeypatch.chdir(tmp_path)


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
    # (environment, db path, declared mount, mount table points, expected)
    cases = [
        ("production", "", None, None, "agent_store_not_configured"),
        (
            "production",
            str(root / "agent.db"),
            str(root),
            [str(root)],
            "agent_store_shared",
        ),
        (
            "production",
            str(root / "agent.db"),
            str(root),
            [],
            "agent_store_mount_unverified",
        ),
        (
            "production",
            str(root / "agent.db"),
            str(tmp_path / "elsewhere"),
            [str(root)],
            "agent_store_root_not_mounted",
        ),
        (
            "production",
            str(root / "agent.db"),
            str(root),
            [str(root), str(root / "bind-mounted-subtree")],
            "agent_store_root_shadowed",
        ),
        (
            "production",
            str(tmp_path / "other" / "agent.db"),
            str(root),
            [str(root)],
            "agent_store_unshared",
        ),
        ("local", "", None, None, "agent_store_not_configured"),
        (
            "local",
            str(root / "agent.db"),
            str(root),
            [str(root)],
            "agent_store_shared",
        ),
        (
            "local",
            str(root / "agent.db"),
            str(root),
            [],
            "agent_store_mount_unverified_dev_only",
        ),
        (
            "local",
            str(tmp_path / "other" / "agent.db"),
            str(root),
            [str(root)],
            "agent_store_unshared_dev_only",
        ),
    ]
    for environment, path, mount, points, _expected in cases:
        monkeypatch.setenv("APP_ENV", environment)
        monkeypatch.setenv("AGENT_STORE_SHARED_ROOT", str(root))
        if mount:
            monkeypatch.setenv("AGENT_STORE_SHARED_MOUNT", mount)
        else:
            monkeypatch.delenv("AGENT_STORE_SHARED_MOUNT", raising=False)
        # A shadow mount inside the root makes the configuration unshared.
        if _expected == "agent_store_root_shadowed":
            monkeypatch.setenv(
                "AGENT_STORE_SHADOW_MOUNTS", str(root / "bind-mounted-subtree")
            )
        else:
            monkeypatch.delenv("AGENT_STORE_SHADOW_MOUNTS", raising=False)
        # The mount table the readiness reads (empty = no evidence).
        monkeypatch.setenv(
            "AGENT_STORE_MOUNTINFO",
            _write_mountinfo(tmp_path, points or []),
        )
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
