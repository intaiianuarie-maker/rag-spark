"""Readiness-probe contract matrix — landed from the M0-003 ad-hoc probe (test1).

Provenance: `/root/.tmp/m004_probe_checks.py` (handoffs/M0-003-backend-test1.md,
probe matrix 11/11). TASK-M0-004 asked for this ad-hoc probe matrix to become a
committed fixture, so the script now lives here and is executed as a subprocess
by `tests/integration/test_landed_probe_matrix.py` (subprocess keeps its global
settings/DATABASE_URL mutations out of the pytest process).

Covers the test1 role meta-checks against the readiness probes:
  - third-party unavailable (connection refused)
  - timeout (server accepts but never responds)
  - malformed/unexpected response (HTTP 500)
  - positive control (HTTP 200)

All probes must RETURN a DependencyStatus, never raise, and honour the deadline.

Run directly (repo root):

    PYTHONPATH=packages:apps/api python3 tests/fixtures/scripts/m004_probe_checks.py

Exit 0 + `PROBE_CHECKS_OK` on success, exit 1 otherwise. No services, weights
or API keys required.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time

TIMEOUT = 1.5
DEAD = "127.0.0.1:59999"  # nothing listens here

results: list[tuple[str, object, float]] = []


def record(case: str, status: object, elapsed: float) -> None:
    from core.health import DependencyStatus

    results.append((case, status, elapsed))
    reachable = getattr(status, "reachable", None)
    error = getattr(status, "error", None) or getattr(status, "detail", None)
    print(
        f"{case:44s} returned={isinstance(status, DependencyStatus)} "
        f"reachable={reachable} elapsed={elapsed:.2f}s "
        f"err/detail={error!r}"
    )


def hang_server() -> tuple[object, int]:
    """TCP server that accepts and never sends a byte."""

    class Hang:
        def __init__(self) -> None:
            self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.srv.bind(("127.0.0.1", 0))
            self.srv.listen(16)
            self.held: list[socket.socket] = []
            self.stop = False

        def run(self) -> None:
            self.srv.settimeout(0.2)
            while not self.stop:
                try:
                    connection, _ = self.srv.accept()
                    connection.setblocking(False)
                    self.held.append(connection)
                except OSError:
                    continue

    server = Hang()
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, server.srv.getsockname()[1]


async def http_status_server(
    status: int, body: str, ctype: str
) -> tuple[asyncio.AbstractServer, int]:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(1024)
        payload = body.encode()
        writer.write(
            b"HTTP/1.1 %d X\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n"
            % (status, ctype.encode(), len(payload))
        )
        writer.write(payload)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def timed(coro: object) -> tuple[object, float]:
    start = time.perf_counter()
    outcome = await coro  # type: ignore[misc]
    return outcome, time.perf_counter() - start


async def main() -> None:
    import app.db as db
    from app.adapters import minio_probe, postgres, qdrant_probe, redis_probe
    from core.config import get_settings
    from core.health import DependencyStatus

    async def set_db_url(url: str) -> None:
        """Point the shared engine at `url` (fresh settings + engine)."""
        await db.dispose_engine()
        get_settings.cache_clear()
        os.environ["DATABASE_URL"] = url

    hang, hang_port = hang_server()
    srv500, port_500 = await http_status_server(500, "boom", "text/plain")
    srv200, port_200 = await http_status_server(200, '{"status":"ok"}', "application/json")

    # 1. unavailable (connection refused)
    await set_db_url(f"postgresql+psycopg://u:p@{DEAD}/x")
    for case, make in [
        ("unavailable/postgres", lambda: postgres.probe(TIMEOUT)),
        ("unavailable/redis", lambda: redis_probe.probe(f"redis://{DEAD}/0", TIMEOUT)),
        ("unavailable/qdrant", lambda: qdrant_probe.probe(f"http://{DEAD}", TIMEOUT)),
        ("unavailable/minio", lambda: minio_probe.probe(f"http://{DEAD}", TIMEOUT)),
    ]:
        outcome, elapsed = await timed(make())
        record(case, outcome, elapsed)

    # 2. timeout (accepts, never responds)
    await set_db_url(f"postgresql+psycopg://u:p@127.0.0.1:{hang_port}/x")
    for case, make in [
        ("timeout/postgres", lambda: postgres.probe(TIMEOUT)),
        (
            "timeout/redis",
            lambda: redis_probe.probe(f"redis://127.0.0.1:{hang_port}/0", TIMEOUT),
        ),
        (
            "timeout/qdrant",
            lambda: qdrant_probe.probe(f"http://127.0.0.1:{hang_port}", TIMEOUT),
        ),
        (
            "timeout/minio",
            lambda: minio_probe.probe(f"http://127.0.0.1:{hang_port}", TIMEOUT),
        ),
    ]:
        outcome, elapsed = await timed(make())
        record(case, outcome, elapsed)

    # 3. malformed / unexpected upstream response
    outcome, elapsed = await timed(qdrant_probe.probe(f"http://127.0.0.1:{port_500}", TIMEOUT))
    record("malformed/qdrant-500", outcome, elapsed)
    outcome, elapsed = await timed(minio_probe.probe(f"http://127.0.0.1:{port_500}", TIMEOUT))
    record("malformed/minio-500", outcome, elapsed)

    # 4. positive control
    outcome, elapsed = await timed(qdrant_probe.probe(f"http://127.0.0.1:{port_200}", TIMEOUT))
    record("positive/qdrant-200", outcome, elapsed)

    await set_db_url(f"postgresql+psycopg://u:p@{DEAD}/x")
    hang.stop = True  # type: ignore[attr-defined]
    srv500.close()
    srv200.close()

    print("\n--- SUMMARY ---")
    raised = [case for case, status, _ in results if not isinstance(status, DependencyStatus)]
    overruns = [case for case, _, elapsed in results if elapsed > TIMEOUT + 1.0]
    negatives = [(case, status) for case, status, _ in results if not case.startswith("positive/")]
    positive_ok = [
        case
        for case, status, _ in results
        if case.startswith("positive/") and getattr(status, "reachable", False)
    ]
    negative_ok = [
        (case, status)
        for case, status in negatives
        if (not getattr(status, "reachable", True))
        and (getattr(status, "error", None) or getattr(status, "detail", None))
    ]
    print(
        f"cases={len(results)} raised={len(raised)} deadline_overruns={len(overruns)} "
        f"positive_reachable={len(positive_ok)}/1 "
        f"negative_unreachable_with_detail={len(negative_ok)}/{len(negatives)}"
    )
    if (
        raised
        or overruns
        or len(positive_ok) != 1
        or len(negative_ok) != len(negatives)
    ):
        raise SystemExit(1)
    print("PROBE_CHECKS_OK")


if __name__ == "__main__":
    asyncio.run(main())
