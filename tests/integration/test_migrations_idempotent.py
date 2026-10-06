"""`alembic upgrade head` idempotency (TASK-M0-004 requirement 3).

Runs the migration CLI twice against the live database and asserts the second
run is a no-op (`Running upgrade` only appears when work happens). Skips loudly
when PostgreSQL or the alembic CLI is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from service_probe import require_service

pytestmark = pytest.mark.integration

DEFAULT_DATABASE_URL = "postgresql+psycopg://knowledge:knowledge@localhost:5432/knowledge"


def _alembic_command() -> list[str]:
    sibling = Path(sys.executable).with_name("alembic")
    if sibling.is_file():
        return [str(sibling)]
    found = shutil.which("alembic")
    if found:
        return [found]
    pytest.skip(
        "alembic CLI not found next to the interpreter or on PATH — "
        "install with: pip install -r requirements-dev.txt"
    )


def _upgrade(repo_root: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    command = [
        *_alembic_command(),
        "-c",
        str(repo_root / "apps" / "api" / "alembic.ini"),
        "upgrade",
        "head",
    ]
    return subprocess.run(
        command,
        cwd=repo_root / "apps" / "api",
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_alembic_upgrade_head_is_idempotent(
    repo_root: Path, subprocess_env: dict[str, str]
) -> None:
    require_service(
        os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        "PostgreSQL",
        "docker compose up -d postgres",
    )

    first = _upgrade(repo_root, subprocess_env)
    assert first.returncode == 0, f"first upgrade failed:\n{first.stdout}\n{first.stderr}"

    second = _upgrade(repo_root, subprocess_env)
    assert second.returncode == 0, f"second upgrade failed:\n{second.stdout}\n{second.stderr}"
    assert "Running upgrade" not in second.stdout, (
        "second `alembic upgrade head` re-applied migrations (not idempotent):\n"
        f"{second.stdout}"
    )
