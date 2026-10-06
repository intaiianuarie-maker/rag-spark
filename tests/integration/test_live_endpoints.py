"""Live system endpoints against the running compose stack (skip-not-pass).

Shape assertions always run when the API is up; the reachability assertion
skips with details when compose is down, so a down stack can never look green.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

DEPENDENCIES = ("postgres", "redis", "qdrant", "minio")


def test_health_is_200_with_service_and_version(api_client) -> None:
    response = api_client.get("/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "knowledge-os-api"
    assert isinstance(body["version"], str) and body["version"]
    assert isinstance(body["env"], str) and body["env"]


def test_version_endpoint_reports_build_info(api_client) -> None:
    response = api_client.get("/version")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["service"] == "knowledge-os-api"
    assert "git_sha" in body and "env" in body


def test_ready_shape_lists_all_four_dependencies(api_client) -> None:
    """`/ready` must name postgres/redis/qdrant/minio whether ok or degraded."""
    response = api_client.get("/ready")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] in {"ok", "degraded"}, body
    assert isinstance(body["ready"], bool), body
    dependencies = body["dependencies"]
    for name in DEPENDENCIES:
        assert name in dependencies, f"/ready omitted {name}: {sorted(dependencies)}"
        assert dependencies[name]["name"] == name
        assert isinstance(dependencies[name]["reachable"], bool)


def test_ready_dependencies_are_reachable_with_compose_up(api_client) -> None:
    body = api_client.get("/ready").json()
    unreachable = {
        name: body["dependencies"][name]
        for name in DEPENDENCIES
        if not body["dependencies"][name]["reachable"]
    }
    if unreachable:
        pytest.skip(f"compose services not reachable, run `make up`: {unreachable}")
    assert body["status"] == "ok"
    assert body["ready"] is True


def test_web_proxy_passes_backend_health_through(web_client) -> None:
    """Live frontend -> proxy -> backend chain on :3001 (compose publishes 3001 here)."""
    response = web_client.get("/api/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, body
    health = body["health"]
    assert health["status"] == "ok", body
    assert health["version"], body
    assert health["isEmpty"] is False, body
    assert isinstance(body["fetchedAt"], str) and body["fetchedAt"]
