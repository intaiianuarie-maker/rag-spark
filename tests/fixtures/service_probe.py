"""Service reachability helpers for the harness (skip-not-pass).

Imported by `tests/conftest.py` and by individual tests that need a live
service. Every refusal to run is an explicit `pytest.skip` naming the service,
the URL and the command that starts it — a down service can never look like a
green test (TASK-M0-004 requirement 1).
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest

# Defaults match the compose stack shipped by M0-003. `web` publishes 3001 on
# this host because :3000 is occupied by an unrelated process (compose default
# stays :3000); override with WEB_PORT/WEB_BASE_URL elsewhere.
DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_WEB_BASE_URL = "http://127.0.0.1:3001"
DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"
DEFAULT_POSTGRES_HOSTPORT = ("127.0.0.1", 5432)


def tcp_open(url: str, timeout: float = 0.75) -> bool:
    """True when something accepts TCP connections on the URL's host:port."""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def require_service(url: str, name: str, start_command: str) -> str:
    """Return `url` when reachable, otherwise skip with an actionable message."""
    if tcp_open(url):
        return url
    pytest.skip(
        f"{name} not reachable at {url} (TCP connect failed) — start it with "
        f"`{start_command}` and re-run. Never silently passing a service-dependent test."
    )


def env_url(name: str, default: str) -> str:
    return os.environ.get(name, default).rstrip("/")
