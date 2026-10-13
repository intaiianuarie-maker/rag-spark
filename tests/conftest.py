"""Shared fixtures for the knowledge-os integration harness (TASK-M0-004).

Design rules (see `docs/engineering/testing-strategy.md` + `.agents/test1-mimo.md`):

- **Skip loudly, never silent-pass.** Every service-dependent fixture refuses to
  run with a message that names the service, the URL and the command that
  starts it.
- **No services for unit/contract tests.** They run on fakes so `pytest -q` is
  green with compose down.
- **No model weights, no API keys, no vendor SDKs** anywhere in `tests/`.
- **A/B workspaces** are provided as a stub so the M1/M3 ACL E2E can be turned
  green without rewriting the harness (TASK-M0-004 requirement 1 + 4).
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
FIXTURES_DIR = TESTS_DIR / "fixtures"
SCRIPTS_DIR = FIXTURES_DIR / "scripts"

# Make the shared test doubles importable as `harness_fakes` from every test
# module (conftest.py of this package is imported before its test modules).
# `pyproject.toml` is outside TASK-M0-004's Allowed Files, so the extra path is
# added here instead of in `[tool.pytest.ini_options].pythonpath`.
if str(FIXTURES_DIR) not in sys.path:
    sys.path.insert(0, str(FIXTURES_DIR))

from object_store_env import apply_if_reachable  # noqa: E402 - same sys.path insert
from service_probe import (  # noqa: E402 - must follow the sys.path insert above
    DEFAULT_API_BASE_URL,
    DEFAULT_WEB_BASE_URL,
    require_service,
)

# T-V1-039: document originals live in object storage, so a host-side call to
# the upload endpoint needs the same S3 env docker-compose.yml gives the
# containers. Applied only when a store is actually listening, so the
# loud-skip rules below still own the "compose is down" case instead of
# turning every upload-driven test into a hard failure.
OBJECT_STORE_ENDPOINT = apply_if_reachable()

# Host notes (handoffs/M0-003-*-test1.md):
# - /tmp is a 100%-full tmpfs here; /root/.tmp lives on the root fs.
# - uvicorn --reload watches the bind-mounted apps/api; writing .pyc there
#   restarts the dev container and briefly 502s the web proxy, so the harness
#   disables bytecode writes for the whole test run.
sys.dont_write_bytecode = True
if "TMPDIR" not in os.environ and Path("/root/.tmp").is_dir():
    os.environ["TMPDIR"] = "/root/.tmp"
    tempfile.tempdir = None

# Same defaults as tests/unit/test_health.py so the app never touches real
# services unless a test explicitly opts in.
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("APP_ENV", "test")


def pytest_configure(config: pytest.Config) -> None:
    # Registered here because `pyproject.toml` is outside TASK-M0-004's
    # Allowed Files and `--strict-markers` rejects unregistered markers.
    config.addinivalue_line(
        "markers", "contract: adapter contract test (fakes only, no services)"
    )
    config.addinivalue_line(
        "markers", "e2e: opt-in end-to-end test (RUN_WEB_E2E=1, needs compose + node)"
    )


# --------------------------------------------------------------------------------------
# Workspace isolation helper stub (A/B pair for the M1/M3 ACL E2E)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Workspace:
    """One workspace as the security E2E will see it (M1 owns the real objects)."""

    id: str
    name: str
    #: Phrases that must never be visible from the other workspace (R-004).
    secret_phrase: str

    @property
    def scope(self):
        from core.auth import ACLScope

        return ACLScope(user_id=f"user-{self.id}", workspace_ids=[self.id])

    @property
    def principal(self):
        from core.auth import Principal, Role

        return Principal(
            user_id=f"user-{self.id}",
            email=f"{self.id}@example.invalid",
            is_authenticated=True,
            workspace_roles={self.id: Role.EDITOR},
        )


@dataclass(frozen=True)
class WorkspacePair:
    """A/B workspaces: A's secret phrase must return zero results for B."""

    a: Workspace
    b: Workspace

    @property
    def all(self) -> tuple[Workspace, Workspace]:
        return (self.a, self.b)


@pytest.fixture
def workspaces() -> WorkspacePair:
    """A/B workspace stub — consumed by the xfail placeholders until M1/M3 land."""
    return WorkspacePair(
        a=Workspace(id="ws-a", name="Alpha", secret_phrase="Aralia secret phrase alpha-7743"),
        b=Workspace(id="ws-b", name="Bravo", secret_phrase="Bravo secret phrase bravo-8812"),
    )


# --------------------------------------------------------------------------------------
# Paths + service URLs
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def api_base_url() -> str:
    return os.environ.get("API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")


@pytest.fixture
def web_base_url() -> str:
    return os.environ.get("WEB_BASE_URL", DEFAULT_WEB_BASE_URL).rstrip("/")


@pytest.fixture
def api_client(api_base_url: str):
    """HTTP client for the live API; skips (loudly) when compose is down."""
    httpx = pytest.importorskip("httpx", reason="httpx is a pinned test dependency")
    require_service(api_base_url, "knowledge-os API", "docker compose up -d")
    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        yield client


@pytest.fixture
def web_client(web_base_url: str):
    """HTTP client for the live web shell; skips (loudly) when compose is down."""
    httpx = pytest.importorskip("httpx", reason="httpx is a pinned test dependency")
    require_service(web_base_url, "knowledge-os web", "docker compose up -d")
    with httpx.Client(base_url=web_base_url, timeout=10.0) as client:
        yield client


@pytest.fixture
def subprocess_env() -> dict[str, str]:
    """Environment for harness subprocesses (probe scripts, alembic, node)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "packages"), str(REPO_ROOT / "apps" / "api")]
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("MIGRATIONS_ON_BOOT", "false")
    env.setdefault("LOG_JSON", "false")
    env.setdefault("APP_ENV", "test")
    env.setdefault("TMPDIR", str(Path.home() / ".tmp"))
    env.setdefault("NEXT_TELEMETRY_DISABLED", "1")
    return env
