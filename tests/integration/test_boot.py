"""Boot/integration tests.

The boot test always runs: `/health` must be 200 and `/ready` must return 200
with per-service reachability entries (non-fatal by design). When compose
services are up, a stricter check asserts every enabled dependency is
reachable; otherwise it skips with a clear message (never a silent pass).

T-V1-027: `DEPENDENCIES` pins the EXACT `/ready` set, identical to the literal
in `tests/fixtures/scripts/m004_ready_degraded.py` and
`tests/integration/test_live_endpoints.py`: the stores (postgres/redis/qdrant/
minio/infinity), the model (`llm_gateway`) and the queue consumer (`worker`),
plus `graph_store` only when the graph mode is enabled (off by default).
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("APP_ENV", "test")
# T-V1-027: give this process the documented gateway config (same default as
# docker-compose.yml / .env.example) so the strict reachability check below
# means "stack up AND every required capability reachable" instead of skipping
# on an unset LITELLM_URL — readiness and LiteLLMProvider then target the same
# gateway, which is the point of probing it.
os.environ.setdefault("LITELLM_URL", "https://openrouter.ai/api/v1")

# T-V1-027 — pinned exact set, same expression in all 3 places.
BASE_DEPENDENCIES = ("postgres", "redis", "qdrant", "minio", "infinity", "llm_gateway", "worker")
GRAPH_ENABLED = os.environ.get("GRAPH_MODE", "off").strip().lower() not in {"", "off"}
DEPENDENCIES = BASE_DEPENDENCIES + (("graph_store",) if GRAPH_ENABLED else ())


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
    # Exact set: no dependency may be added to or dropped from /ready without
    # updating this contract (and the two sibling pins).
    assert set(body["dependencies"]) == set(DEPENDENCIES)
    assert tuple(body["dependencies"]) == probe_names_for_current_mode()
    for name in DEPENDENCIES:
        dependency = body["dependencies"][name]
        assert dependency["name"] == name
        assert isinstance(dependency["reachable"], bool)


def probe_names_for_current_mode() -> tuple[str, ...]:
    """The names the implementation reports for the mode this process runs with."""
    from app.adapters.readiness import probe_names
    from core.config import get_settings

    return probe_names(get_settings().graph_mode)


@pytest.mark.integration
def test_ready_probe_set_matches_pinned_contract() -> None:
    """The implementation's set is this file's literal, for every GRAPH_MODE."""
    from app.adapters.readiness import probe_names

    assert probe_names("off") == BASE_DEPENDENCIES
    assert probe_names("") == BASE_DEPENDENCIES
    # Graph enabled — and a typo'd GRAPH_MODE too, which must still be
    # reported (red) instead of silently dropped.
    for mode in ("rule", "llm", "banana"):
        assert probe_names(mode) == (*BASE_DEPENDENCIES, "graph_store")


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
