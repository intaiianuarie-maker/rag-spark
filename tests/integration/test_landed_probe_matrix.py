"""Landed M0-003 probe matrix (11 cases) run from the committed fixture.

Provenance: `/root/.tmp/m004_probe_checks.py` →
`tests/fixtures/scripts/m004_probe_checks.py` (TASK-M0-004: "land your ad-hoc
probe matrix as a committed fixture"). Runs as a subprocess so its settings and
`DATABASE_URL` mutations stay out of the pytest process. Needs no compose: it
points every probe at closed/hanging/stubbed local sockets.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

SCRIPT = Path("tests") / "fixtures" / "scripts" / "m004_probe_checks.py"


def test_probe_matrix_passes(repo_root: Path, subprocess_env: dict[str, str]) -> None:
    script = repo_root / SCRIPT
    assert script.is_file(), f"committed probe fixture missing: {script}"
    run = subprocess.run(
        [sys.executable, str(script)],
        cwd=repo_root,
        env=subprocess_env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = f"{run.stdout}{run.stderr}"
    assert run.returncode == 0, f"probe matrix failed:\n{output}"
    assert "PROBE_CHECKS_OK" in run.stdout, output
    assert "cases=11" in run.stdout, run.stdout
    assert "raised=0" in run.stdout, run.stdout
    assert "deadline_overruns=0" in run.stdout, run.stdout
    # Every one of the 11 probes must return a DependencyStatus (never raise).
    assert run.stdout.count("returned=True") == 11, run.stdout
    assert run.stdout.count("reachable=True") == 1, (
        f"only the positive control may be reachable:\n{run.stdout}"
    )


def test_probe_matrix_covers_every_declared_case(
    repo_root: Path, subprocess_env: dict[str, str]
) -> None:
    """Evidence check: all 11 named cases from the M0-003 report actually ran."""
    cases = [
        "unavailable/postgres",
        "unavailable/redis",
        "unavailable/qdrant",
        "unavailable/minio",
        "timeout/postgres",
        "timeout/redis",
        "timeout/qdrant",
        "timeout/minio",
        "malformed/qdrant-500",
        "malformed/minio-500",
        "positive/qdrant-200",
    ]
    run = subprocess.run(
        [sys.executable, str(repo_root / SCRIPT)],
        cwd=repo_root,
        env=subprocess_env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert run.returncode == 0, f"{run.stdout}{run.stderr}"
    for case in cases:
        assert case in run.stdout, f"case {case} missing from the probe matrix:\n{run.stdout}"
