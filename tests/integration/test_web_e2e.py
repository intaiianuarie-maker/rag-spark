"""Opt-in web e2e: production `next build` + the landed proxy-branch matrix.

Gated behind `RUN_WEB_E2E=1` because it needs node/npm, a production build and
the compose API on :8000 (default `pytest -q` and `pytest -m "not e2e"` stay
service-free per tests/README.md). CI already runs `npm run build` in its
frontend job; this is the local/verification path.

Fixtures landed from the M0-003 ad-hoc run:
`tests/fixtures/scripts/m004_proxy_branches.sh` + `m004_stubs.py`
(handoffs/M0-003-frontend-test1.md, proxy matrix 5/5).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from service_probe import require_service

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

ENABLED = os.environ.get("RUN_WEB_E2E") == "1"
REASON = "RUN_WEB_E2E!=1 — opt-in web e2e (needs node/npm, `next build` and the compose API)"


def _npm() -> str:
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm not installed — web e2e not run (install Node 22 to enable it)")
    return npm


def _run_npm_build(repo_root: Path, env: dict[str, str]) -> None:
    build = subprocess.run(
        [_npm(), "run", "build"],
        cwd=repo_root / "apps" / "web",
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert build.returncode == 0, f"next build failed:\n{build.stdout}\n{build.stderr}"


@pytest.mark.skipif(not ENABLED, reason=REASON)
def test_next_build_succeeds(repo_root: Path, subprocess_env: dict[str, str]) -> None:
    _run_npm_build(repo_root, subprocess_env)
    assert (repo_root / "apps" / "web" / ".next" / "BUILD_ID").is_file(), (
        "next build produced no production bundle"
    )


@pytest.mark.skipif(not ENABLED, reason=REASON)
def test_proxy_branch_matrix(repo_root: Path, subprocess_env: dict[str, str]) -> None:
    """All five `/api/health` proxy branches, isolated per case (ports 3111-3115)."""
    require_service(
        os.environ.get("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        "knowledge-os API",
        "docker compose up -d api",
    )
    if not (repo_root / "apps" / "web" / ".next" / "BUILD_ID").is_file():
        _run_npm_build(repo_root, subprocess_env)

    script = repo_root / "tests" / "fixtures" / "scripts" / "m004_proxy_branches.sh"
    assert script.is_file(), f"committed proxy matrix fixture missing: {script}"
    run = subprocess.run(
        ["bash", str(script)],
        cwd=repo_root,
        env=subprocess_env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    output = f"{run.stdout}{run.stderr}"
    assert run.returncode == 0, f"proxy branch matrix failed:\n{output}"
    assert "PROXY_BRANCHES_OK" in run.stdout, output
    for case in (
        "A_healthy_passthrough",
        "B_backend_down_502",
        "C_non2xx_passthrough",
        "D_non_json_502",
        "E_timeout_502",
    ):
        assert f"PASS  {case}" in run.stdout, f"{case} did not pass:\n{run.stdout}"
