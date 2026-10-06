"""Frontend <-> backend health payload cross-contract (M0-003 review follow-up).

The M0-003 frontend review asked M0-004 to lock both sides of the `/api/health`
proxy: the backend owns `GET /health`, the status page trusts the guard in
`apps/web/src/lib/health.ts`. This test compiles that guard with the web app's
own `tsc` (this host's node has no TypeScript support), feeds it the *real*
backend payload plus the documented unhealthy/malformed shapes, and asserts the
guard accepts what it must and rejects the rest.

Skips loudly when node or the web toolchain is missing — never silent-passes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]


@pytest.fixture(scope="module")
def backend_health_payload() -> dict:
    """The real `GET /health` payload, produced by the app itself (no compose)."""
    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok", payload
    assert payload["service"] == "knowledge-os-api", payload
    assert isinstance(payload["version"], str) and payload["version"], payload
    assert isinstance(payload["env"], str) and payload["env"], payload
    return payload


def _compile_health_guard(repo_root: Path, tmp_path: Path) -> Path:
    tsc = repo_root / "apps" / "web" / "node_modules" / ".bin" / "tsc"
    if not tsc.is_file():
        pytest.skip(
            f"typescript compiler not found at {tsc} — run: cd apps/web && npm ci "
            "(frontend guard check not run)"
        )
    source = repo_root / "apps" / "web" / "src" / "lib" / "health.ts"
    out_dir = tmp_path / "health_guard"
    out_dir.mkdir()
    compile_run = subprocess.run(
        [
            str(tsc),
            str(source),
            "--outDir",
            str(out_dir),
            "--module",
            "esnext",
            "--target",
            "es2021",
            "--moduleResolution",
            "bundler",
            "--skipLibCheck",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert compile_run.returncode == 0, (
        f"tsc failed to compile health.ts:\n{compile_run.stdout}\n{compile_run.stderr}"
    )
    compiled = out_dir / "health.js"
    assert compiled.is_file(), f"tsc produced no output at {compiled}"
    (out_dir / "package.json").write_text('{\n  "type": "module"\n}\n', encoding="utf-8")
    return compiled


def test_backend_payload_passes_the_frontend_guard(
    repo_root: Path, tmp_path: Path, backend_health_payload: dict
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime not installed — frontend guard check not run")

    compiled = _compile_health_guard(repo_root, tmp_path)
    payload_file = tmp_path / "backend_health.json"
    payload_file.write_text(json.dumps(backend_health_payload), encoding="utf-8")

    script = repo_root / "tests" / "fixtures" / "scripts" / "health_guard_check.mjs"
    guard_run = subprocess.run(
        [node, str(script), str(compiled), str(payload_file)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = f"{guard_run.stdout}{guard_run.stderr}".strip()
    assert guard_run.returncode == 0, f"payload guard rejected the contract:\n{output}"
    assert "HEALTH_GUARD_OK" in output, output


def test_proxy_error_kinds_are_declared_in_the_frontend_guard(repo_root: Path) -> None:
    """The three proxy error kinds are a shared contract: source + guard must agree."""
    guard_source = (repo_root / "apps" / "web" / "src" / "lib" / "health.ts").read_text(
        encoding="utf-8"
    )
    route_source = (repo_root / "apps" / "web" / "src" / "app" / "api" / "health" / "route.ts"
                    ).read_text(encoding="utf-8")
    for kind in ("backend_unreachable", "backend_unhealthy", "unexpected_response"):
        assert kind in guard_source, f"{kind} missing from health.ts guard"
        assert kind in route_source, f"{kind} never produced by the proxy route"
