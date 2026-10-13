"""T-V1-056 integration: the worker liveness heartbeat against a **live** Redis.

The unit file pins both directions against a stub. This one proves the same
verdicts through the real broker and the real expiry clock, so "the key
vanished because the worker stopped" is observed rather than simulated with an
explicit ``DEL``:

* a worker that has just renewed its heartbeat reads **reachable**;
* once it stops renewing, the key expires on its own and the probe reads
  **not reachable** — the fail-closed direction ``/ready`` needs.

Skipped when Redis is unreachable. Keys live under a unique per-test
``INGEST_QUEUE_PREFIX`` so a live worker on the shared instance is never
touched.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid

import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.integration

REDIS_URL = "redis://127.0.0.1:6379/0"
TTL_ENV = "INGEST_WORKER_HEARTBEAT_TTL_SECONDS"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.fixture
def live_redis(monkeypatch: pytest.MonkeyPatch):
    """A unique queue prefix so the shared broker's real worker is untouched."""
    if not _port_open(6379):
        pytest.skip("redis not reachable; run `make up`")
    monkeypatch.setenv("INGEST_QUEUE_PREFIX", f"t056-{uuid.uuid4().hex[:12]}:")
    return REDIS_URL


async def _probe(redis_url: str):
    import app.adapters.readiness as readiness

    return await readiness.worker_liveness.probe(redis_url, 2.0)


async def test_a_running_worker_reads_reachable(live_redis: str) -> None:
    """HEALTHY direction, through the real writer and the real broker."""
    import jobs.ingest_worker as ingest_worker

    monkeypatch_ttl = 2
    os.environ[TTL_ENV] = str(monkeypatch_ttl)
    try:
        await ingest_worker.write_heartbeat()  # the real writer, real Redis
        status = await _probe(live_redis)
    finally:
        os.environ.pop(TTL_ENV, None)

    assert status.reachable is True, f"expected reachable, got {status.error!r}"
    assert status.detail and "heartbeat" in status.detail


async def test_a_stopped_worker_reads_not_reachable_once_its_ttl_lapses(
    live_redis: str,
) -> None:
    """STOPPED direction, driven by the real expiry clock — not a manual DEL.

    The writer is used with a sub-second TTL and then simply not called again,
    which is exactly what a stopped worker does. When the key expires, the
    probe must fail closed.
    """
    import app.adapters.readiness as readiness
    import jobs.ingest_worker as ingest_worker

    os.environ[TTL_ENV] = "0.3"  # bound = 0.6s, so the lapse is observable fast
    try:
        await ingest_worker.write_heartbeat()
        live = await _probe(live_redis)
        assert live.reachable is True, "precondition: the worker is up"

        # The worker is now "stopped": nothing renews, Redis expires the key.
        await asyncio.sleep(1.0)

        dead = await _probe(live_redis)
    finally:
        os.environ.pop(TTL_ENV, None)

    assert dead.reachable is not True, (
        "a worker that stopped renewing must not read ready "
        f"(got detail={dead.detail!r})"
    )
    assert dead.error and "heartbeat" in dead.error
    # The dependency is REQUIRED for the ingest mode, so a non-reachable worker
    # is what /ready turns into ready:false / DEGRADED (readiness.all(reachable)).
    assert readiness.Dependency.WORKER.value == "worker"


async def test_an_absent_key_fails_closed_on_the_live_broker(live_redis: str) -> None:
    """The bare fail-closed case: nothing was ever written under this prefix."""
    status = await _probe(live_redis)

    assert status.reachable is not True
    assert status.error and "heartbeat" in status.error
