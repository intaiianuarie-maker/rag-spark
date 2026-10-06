"""Landed `/ready` degraded-contract check run from the committed fixture.

Provenance: `/root/.tmp/m004_ready_degraded.py` →
`tests/fixtures/scripts/m004_ready_degraded.py` (handoffs/M0-003-backend-test1.md,
`DEGRADED_CHECKS_OK`). All four dependencies point at a closed port, so the app
must answer 200 with `status=degraded` / `ready=false` / per-dependency error
details while liveness stays green. No compose needed — this *is* the
compose-down scenario.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

SCRIPT = Path("tests") / "fixtures" / "scripts" / "m004_ready_degraded.py"


def test_ready_degraded_contract_with_all_dependencies_down(
    repo_root: Path, subprocess_env: dict[str, str]
) -> None:
    script = repo_root / SCRIPT
    assert script.is_file(), f"committed degraded-check fixture missing: {script}"
    run = subprocess.run(
        [sys.executable, str(script)],
        cwd=repo_root,
        env=subprocess_env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = f"{run.stdout}{run.stderr}"
    assert run.returncode == 0, f"degraded /ready contract broken:\n{output}"
    assert "DEGRADED_CHECKS_OK" in run.stdout, output
    # Liveness must survive a dead dependency set.
    assert "/health=200" in run.stdout, run.stdout
    assert "/version=200" in run.stdout, run.stdout
    assert "/ready=200" in run.stdout, run.stdout
    assert "status=degraded" in run.stdout or "'status': 'degraded'" in run.stdout, run.stdout
    # Every dependency must be reported unreachable *with* an error detail.
    for name in ("postgres", "redis", "qdrant", "minio"):
        assert f"'name': '{name}'" in run.stdout, f"{name} missing:\n{run.stdout}"
