"""Opt-in real browser journey against an authenticated running release stack."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


@pytest.mark.skipif(os.environ.get("RUN_RELEASE_E2E") != "1", reason="RUN_RELEASE_E2E!=1")
def test_release_journey(repo_root: Path, subprocess_env: dict[str, str]) -> None:
    for name in ("WORKSPACE_ID", "CREDENTIAL_FILE"):
        assert subprocess_env.get(name), f"{name} required for live release acceptance"
    script = repo_root / "tests" / "fixtures" / "scripts" / "release_journey.sh"
    assert script.is_file()
    result = subprocess.run(["bash", str(script)], cwd=repo_root, env=subprocess_env,
                            capture_output=True, text=True, timeout=1800)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RELEASE_JOURNEY_BROWSER_OK" in result.stdout
    assert '"formats_ready":6' in result.stdout
    assert '"citation_passage":true' in result.stdout
    assert '"keyboard":true' in result.stdout
    for mode in ("hybrid", "semantic", "keyword"):
        assert f'"{mode}"' in result.stdout
