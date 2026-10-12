"""Live system endpoints against the running compose stack (skip-not-pass).

Shape assertions always run when the API is up; the reachability assertion
skips with details when compose is down, so a down stack can never look green.

T-V1-027: `DEPENDENCIES` pins the EXACT `/ready` set — the same literal as
`tests/fixtures/scripts/m004_ready_degraded.py` and
`tests/integration/test_boot.py` (stores + `llm_gateway` + `worker`, plus
`graph_store` only when the graph mode is enabled). Compose never sets
`GRAPH_MODE`, so the live set is the seven base dependencies.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

# T-V1-027 — pinned exact set, same expression in all 3 places.
BASE_DEPENDENCIES = ("postgres", "redis", "qdrant", "minio", "infinity", "llm_gateway", "worker")
GRAPH_ENABLED = os.environ.get("GRAPH_MODE", "off").strip().lower() not in {"", "off"}
DEPENDENCIES = BASE_DEPENDENCIES + (("graph_store",) if GRAPH_ENABLED else ())


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


def test_ready_shape_lists_every_enabled_dependency(api_client) -> None:
    """`/ready` must name exactly the pinned set, whether ok or degraded."""
    response = api_client.get("/ready")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] in {"ok", "degraded"}, body
    assert isinstance(body["ready"], bool), body
    dependencies = body["dependencies"]
    # Exact set: a new required dependency must be pinned here too (and in the
    # two sibling contracts) — a silently added or dropped check is a failure.
    assert set(dependencies) == set(DEPENDENCIES), (
        f"/ready deps={sorted(dependencies)} expected {sorted(DEPENDENCIES)}"
    )
    for name in DEPENDENCIES:
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


# --- T-V1-005: infinity probe truth table (a 5xx service reads red) ----------


async def _probe_stubbed(status: int | None):
    """Run the infinity probe against a local stub; ``None`` = refused port.

    Stdlib ``ThreadingHTTPServer`` on 127.0.0.1: no compose stack, no network,
    deterministic status codes.
    """
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from app.adapters.infinity_probe import probe

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # stdlib handler entrypoint
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    if status is None:
        # Reserve a port, then close it: the probe meets a refused connection.
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            host, port = reserved.getsockname()
        return await probe(f"http://{host}:{port}", 5.0)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        return await probe(f"http://{host}:{port}", 5.0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("status", [200, 307])
async def test_infinity_probe_below_500_is_reachable(status: int) -> None:
    """Truth table: 200 and 307 (infinity root redirect) count as reachable."""
    result = await _probe_stubbed(status)
    assert result.name == "infinity"
    assert result.reachable is True
    assert result.detail == f"GET / -> {status}"
    assert result.error is None


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_infinity_probe_5xx_is_unreachable(status: int) -> None:
    """T-V1-005: a broken embeddings service must read red, not green."""
    result = await _probe_stubbed(status)
    assert result.reachable is False
    assert result.detail == f"GET / -> {status}"


async def test_infinity_probe_connection_refused_is_unreachable() -> None:
    """A dead (connection-refused) infinity stays unreachable."""
    result = await _probe_stubbed(None)
    assert result.reachable is False
    assert result.error  # transport failure still reports why
