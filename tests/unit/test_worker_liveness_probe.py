"""T-V1-056 — the ``/ready`` worker probe reflects the T-V1-020 claim model.

Before T-V1-020 the worker's entrypoint blocked on ``BRPOP``, so readiness
could prove liveness by finding a blocked ``brpop`` client in ``CLIENT LIST``.
Durable claim/ack replaced that with short non-blocking polls: a **healthy**
worker holds no standing list-pop consumer, so that probe reported it as down —
a false red that would also mask a genuinely dead one behind a permanently red
check.

The worker now renews a short-TTL heartbeat key once per loop iteration and the
probe reads that key's freshness, failing closed when it is absent, stale or
unreadable.

Everything here is service-free: a stub Redis client stands in for the broker,
and time is pinned by writing explicit timestamps rather than sleeping.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

import app.adapters.readiness as readiness
import jobs.ingest_worker as ingest_worker

pytestmark = pytest.mark.unit

TTL_ENV = "INGEST_WORKER_HEARTBEAT_TTL_SECONDS"


class _FakeRedis:
    """Enough of ``redis.asyncio.Redis`` for the heartbeat read/write."""

    def __init__(self, store: dict | None = None, error: Exception | None = None) -> None:
        self.store = store if store is not None else {}
        self.error = error
        self.closed = False
        self.expiry: dict[str, int | None] = {}

    async def get(self, key: str):
        if self.error is not None:
            raise self.error
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, px: int | None = None):
        if self.error is not None:
            raise self.error
        if ex == 0 or px == 0:
            raise ValueError("invalid expire time")  # Redis rejects a zero TTL
        self.store[key] = value
        self.expiry[key] = px if px is not None else ex

    async def aclose(self) -> None:
        self.closed = True


def _install(monkeypatch: pytest.MonkeyPatch, client: _FakeRedis) -> None:
    """Point the readiness module's Redis factory at the stub."""
    monkeypatch.setattr(readiness.redis_asyncio, "from_url", lambda *a, **k: client)


def _fresh(seconds_ago: float = 1.0) -> str:
    return f"{time.time() - seconds_ago:.3f}"


async def _probe() -> object:
    return await readiness.worker_liveness.probe("redis://stub", 1.0)


# --------------------------------------------------------------------------------------
# 1. The key contract: writer and reader cannot drift
# --------------------------------------------------------------------------------------


def test_the_worker_and_the_probe_use_the_same_key() -> None:
    """One prefix, one key — otherwise the probe can never see the signal."""
    assert ingest_worker.heartbeat_key() == readiness._worker_heartbeat_key()
    assert ingest_worker.heartbeat_key().endswith("worker_heartbeat")


def test_the_key_is_namespaced_like_the_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INGEST_QUEUE_PREFIX", "t056-abc:")
    assert ingest_worker.heartbeat_key() == "ingest:t056-abc:worker_heartbeat"
    assert ingest_worker.heartbeat_key() == readiness._worker_heartbeat_key()


def test_the_probe_bound_is_twice_the_worker_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """One env var moves both sides, so no window opens where they disagree."""
    monkeypatch.setenv(TTL_ENV, "4")
    assert ingest_worker.heartbeat_ttl_seconds() == 4.0
    assert readiness._worker_heartbeat_bound() == 8.0

    monkeypatch.delenv(TTL_ENV)
    assert ingest_worker.heartbeat_ttl_seconds() == 15.0
    assert readiness._worker_heartbeat_bound() == 30.0


def test_a_bogus_ttl_falls_back_instead_of_breaking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TTL_ENV, "not-a-number")
    assert ingest_worker.heartbeat_ttl_seconds() == 15.0
    assert readiness._worker_heartbeat_bound() == 30.0


# --------------------------------------------------------------------------------------
# 2. Both directions, pinned
# --------------------------------------------------------------------------------------


async def test_a_fresh_heartbeat_reads_as_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """HEALTHY DIRECTION: a worker renewing its heartbeat is up."""
    client = _FakeRedis({ingest_worker.heartbeat_key(): _fresh()})
    _install(monkeypatch, client)

    status = await _probe()

    assert status.reachable is True
    assert "heartbeat" in (status.detail or "")
    assert client.closed, "the probe must not leak connections"


async def test_an_absent_heartbeat_is_not_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """STOPPED DIRECTION: nothing renewing ⇒ the worker is gone ⇒ fail closed.

    This is what a stopped worker looks like once its TTL lapses: the key has
    expired, so ``GET`` returns nothing.
    """
    _install(monkeypatch, _FakeRedis({}))  # worker stopped, TTL already lapsed

    status = await _probe()

    assert status.reachable is not True, "a stopped worker must never read ready"
    assert status.error and "heartbeat" in status.error


async def test_a_stale_heartbeat_is_not_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key written long ago (or with an over-long TTL) still fails closed."""
    monkeypatch.setenv(TTL_ENV, "5")  # bound = 10s
    client = _FakeRedis({ingest_worker.heartbeat_key(): _fresh(seconds_ago=90)})
    _install(monkeypatch, client)

    status = await _probe()

    assert status.reachable is not True
    assert status.error and "stale" in status.error


async def test_an_heartbeat_just_inside_the_bound_still_reads_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grace window: one missed refresh must not flap the probe red."""
    monkeypatch.setenv(TTL_ENV, "5")  # bound = 10s
    client = _FakeRedis({ingest_worker.heartbeat_key(): _fresh(seconds_ago=9)})
    _install(monkeypatch, client)

    status = await _probe()

    assert status.reachable is True


async def test_an_unreadable_heartbeat_is_not_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage in the key is treated as no signal, never as a live worker."""
    client = _FakeRedis({ingest_worker.heartbeat_key(): "not-a-timestamp"})
    _install(monkeypatch, client)

    status = await _probe()

    assert status.reachable is not True
    assert status.error and "unreadable" in status.error


async def test_a_broker_failure_is_not_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis down ⇒ the worker check fails closed, like every other probe."""
    _install(monkeypatch, _FakeRedis(error=ConnectionError("broker down")))

    status = await _probe()

    assert status.reachable is not True
    assert status.error


async def test_the_probe_never_hunts_for_a_brpop_client() -> None:
    """The stale observable is gone for good, not just currently unused.

    Checks the executable code (``CLIENT LIST`` call), not the prose — the
    docstring deliberately explains *why* the old check was wrong.
    """
    import ast
    import inspect

    source = inspect.getsource(readiness._WorkerLivenessProbe)
    calls = {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "client_list" not in calls, "the BRPOP/CLIENT LIST probe must stay gone"
    assert "get" in calls, "the heartbeat read is the liveness signal now"


# --------------------------------------------------------------------------------------
# 3. The writer side
# --------------------------------------------------------------------------------------


async def test_write_heartbeat_renews_the_key_with_a_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRedis()
    monkeypatch.setattr(ingest_worker, "get_client", lambda: client)

    await ingest_worker.write_heartbeat()

    key = ingest_worker.heartbeat_key()
    assert key in client.store
    assert float(client.store[key]) <= time.time() + 1
    # Millisecond precision: a sub-second TTL must not truncate to EX 0.
    assert client.expiry[key] == int(ingest_worker.heartbeat_ttl_seconds() * 1000)


async def test_write_heartbeat_survives_a_broker_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broker blip must never kill the loop — the redis probe is red anyway."""
    monkeypatch.setattr(
        ingest_worker, "get_client", lambda: _FakeRedis(error=ConnectionError("down"))
    )

    await ingest_worker.write_heartbeat()  # must not raise


async def test_a_sub_second_ttl_is_written_in_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis rejects ``EX 0``, so a fractional TTL must not truncate to seconds.

    The live test uses a 0.3s TTL to observe the expiry clock; without
    millisecond precision that write fails and the heartbeat never lands.
    """
    client = _FakeRedis()
    monkeypatch.setattr(ingest_worker, "get_client", lambda: client)
    monkeypatch.setenv(TTL_ENV, "0.3")

    await ingest_worker.write_heartbeat()

    assert client.expiry[ingest_worker.heartbeat_key()] == 300


async def test_run_worker_renews_the_heartbeat_every_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop owns the heartbeat: one renewal per tick, before anything else."""
    client = _FakeRedis()
    monkeypatch.setattr(ingest_worker, "get_client", lambda: client)
    stop = asyncio.Event()

    async def _promote() -> None:
        stop.set()  # end the loop after this full iteration

    async def _nothing(*args: object, **kwargs: object):
        return None

    monkeypatch.setattr(ingest_worker, "promote_due", _promote)
    monkeypatch.setattr(ingest_worker, "recover_expired", _nothing)
    monkeypatch.setattr(ingest_worker, "dequeue", _nothing)

    await asyncio.wait_for(ingest_worker.run_worker(stop), timeout=5)

    assert ingest_worker.heartbeat_key() in client.store, "the loop must renew liveness"


async def test_a_loop_that_never_starts_renews_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop-before-start must not touch the broker (the no-services contract)."""
    client = _FakeRedis()
    monkeypatch.setattr(ingest_worker, "get_client", lambda: client)

    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(ingest_worker.run_worker(stop), timeout=5)

    assert client.store == {}, "an already-stopped loop must not write to the broker"
