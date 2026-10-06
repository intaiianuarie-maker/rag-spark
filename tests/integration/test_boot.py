"""Boot/integration tests.

The boot test always runs: `/health` must be 200 and `/ready` must return 200
with per-service reachability entries (non-fatal by design). When compose
services are up, a stricter check asserts all four are reachable; otherwise it
skips with a clear message (never a silent pass).
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("APP_ENV", "test")

DEPENDENCIES = ("postgres", "redis", "qdrant", "minio")


@pytest.mark.integration
def test_boot_health_and_ready_shape() -> None:
    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        health = client.get("/health")
        assert health.status_code == 200

        version = client.get("/version")
        assert version.status_code == 200
        assert version.json()["service"] == "knowledge-os-api"

        ready = client.get("/ready")
        assert ready.status_code == 200

    body = ready.json()
    assert body["status"] in {"ok", "degraded"}
    assert isinstance(body["ready"], bool)
    assert set(body["dependencies"]).issuperset(DEPENDENCIES)
    for name in DEPENDENCIES:
        dependency = body["dependencies"][name]
        assert dependency["name"] == name
        assert isinstance(dependency["reachable"], bool)


@pytest.mark.integration
def test_ready_reports_all_dependencies_reachable() -> None:
    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        body = client.get("/ready").json()

    unreachable = [
        name for name in DEPENDENCIES if not body["dependencies"][name]["reachable"]
    ]
    if unreachable:
        details = {name: body["dependencies"][name] for name in unreachable}
        pytest.skip(f"compose services not reachable, run `make up`: {details}")

    assert body["status"] == "ok"
    assert body["ready"] is True
