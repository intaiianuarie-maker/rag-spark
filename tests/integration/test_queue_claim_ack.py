"""T-V1-020 integration: the real Redis claim/ack/lease/recover cycle.

These pin the Lua scripts in ``ingestion.adapters.redis_queue`` against a live
Redis (the unit file pins the worker wiring around them). Skipped when Redis
is unreachable; keys live under a unique per-test prefix so a live worker on
the shared instance is never touched.
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


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.fixture
async def queue_mod(monkeypatch: pytest.MonkeyPatch):
    if not _port_open(6379):
        pytest.skip("redis not reachable; run `make up`")

    import ingestion.adapters.redis_queue as rq

    prefix = f"t020-{uuid.uuid4().hex[:12]}:"
    monkeypatch.setenv("INGEST_QUEUE_PREFIX", prefix)
    # Short default lease so claim and renew agree on the same duration
    # (renew extends by the configured lease, not the per-claim one).
    monkeypatch.setenv("INGEST_QUEUE_LEASE_SECONDS", "0.05")
    try:
        yield rq
    finally:
        client = rq.get_client()
        keys = [
            rq.queue_key(),
            rq.delayed_key(),
            rq.claims_key(),
            rq.claim_tokens_key(),
        ]
        await client.delete(*keys)
        await rq.close_client()


async def test_claim_moves_job_from_ready_to_claims_with_a_token(queue_mod) -> None:
    rq = queue_mod
    job = uuid.uuid4().hex
    await rq.enqueue(job)

    claimed = await rq.claim(timeout_seconds=1, lease=30)
    assert claimed is not None
    assert str(claimed) == job
    assert claimed.token

    client = rq.get_client()
    assert await client.llen(rq.queue_key()) == 0  # off the ready list
    assert await client.zscore(rq.claims_key(), job) is not None  # on the claims zset
    assert await client.hget(rq.claim_tokens_key(), job) == claimed.token


async def test_ack_releases_the_claim_only_with_the_current_token(queue_mod) -> None:
    rq = queue_mod
    job = uuid.uuid4().hex
    await rq.enqueue(job)
    claimed = await rq.claim(timeout_seconds=1, lease=30)
    assert claimed is not None

    # A forged/stale token cannot drop the claim (lease fencing).
    forged = rq.Claim(str(claimed), "not-the-token")
    assert await rq.ack(forged) is False

    client = rq.get_client()
    assert await client.zscore(rq.claims_key(), job) is not None  # claim survived

    assert await rq.ack(claimed) is True
    assert await client.zscore(rq.claims_key(), job) is None
    assert await client.hget(rq.claim_tokens_key(), job) is None


async def test_expired_claim_is_recovered_and_redelivered(queue_mod) -> None:
    rq = queue_mod
    job = uuid.uuid4().hex
    await rq.enqueue(job)

    first = await rq.claim(timeout_seconds=1, lease=0.05)
    assert first is not None

    # Not yet expired: recovery must not touch it.
    assert await rq.recover_expired() == 0

    await asyncio.sleep(0.06)
    assert await rq.recover_expired() == 1

    client = rq.get_client()
    assert await client.llen(rq.queue_key()) == 1  # back on the ready list
    assert await client.zscore(rq.claims_key(), job) is None
    assert await client.hget(rq.claim_tokens_key(), job) is None

    # Redelivered: claimable again, with a NEW token (at-least-once).
    second = await rq.claim(timeout_seconds=1, lease=30)
    assert second is not None
    assert str(second) == job
    assert second.token != first.token

    # The zombie holder's ack (old token) is fenced out; the new claim stands.
    assert await rq.ack(first) is False
    assert await client.zscore(rq.claims_key(), job) is not None
    assert await rq.ack(second) is True


async def test_renew_extends_the_lease_and_is_token_fenced(queue_mod) -> None:
    rq = queue_mod
    job = uuid.uuid4().hex
    await rq.enqueue(job)
    claimed = await rq.claim(timeout_seconds=1)  # env lease: 0.05s
    assert claimed is not None

    assert await rq.renew(claimed) is True
    client = rq.get_client()
    deadline = await client.zscore(rq.claims_key(), job)
    assert deadline is not None and deadline > rq._now() + 0.04

    # Fenced renew: a stale holder cannot extend a re-claimed job's lease.
    await asyncio.sleep(0.06)
    assert await rq.recover_expired() == 1
    second = await rq.claim(timeout_seconds=1, lease=30)
    assert second is not None
    assert await rq.renew(claimed) is False  # old token refused
    assert await rq.renew(second) is True


async def test_claim_is_atomic_under_concurrent_workers(queue_mod) -> None:
    """N workers, one job: exactly one claim wins; the rest time out empty."""
    rq = queue_mod
    job = uuid.uuid4().hex
    await rq.enqueue(job)

    results = await asyncio.gather(
        *(rq.claim(timeout_seconds=1, lease=30) for _ in range(5))
    )
    won = [c for c in results if c is not None]
    assert len(won) == 1
    assert str(won[0]) == job

    client = rq.get_client()
    assert await client.zcard(rq.claims_key()) == 1


async def test_full_cycle_enqueue_claim_ack_and_no_orphan_keys(queue_mod) -> None:
    """claim→ack leaves no residue; the queue is reusable afterwards."""
    rq = queue_mod
    first_job = uuid.uuid4().hex
    second_job = uuid.uuid4().hex
    await rq.enqueue(first_job)
    await rq.enqueue(second_job)  # LPUSH + RPOP = FIFO: first-enqueued first

    c1 = await rq.claim(timeout_seconds=1, lease=30)
    assert c1 is not None and str(c1) == first_job
    assert await rq.ack(c1) is True

    c2 = await rq.claim(timeout_seconds=1, lease=30)
    assert c2 is not None and str(c2) == second_job
    assert await rq.ack(c2) is True

    client = rq.get_client()
    assert await client.llen(rq.queue_key()) == 0
    assert await client.zcard(rq.claims_key()) == 0
    assert await client.hlen(rq.claim_tokens_key()) == 0


async def test_delayed_retry_promotes_back_to_the_claimable_queue(queue_mod) -> None:
    """record_failure's retry path (schedule_retry→promote_due) interoperates
    with claims: a promoted job is claimable again, and its old expired claim
    (if any) cannot ack it."""
    rq = queue_mod
    job = uuid.uuid4().hex
    claimed = await rq.claim(timeout_seconds=0, lease=30)  # empty queue → None
    assert claimed is None

    await rq.enqueue(job)
    claimed = await rq.claim(timeout_seconds=1, lease=30)
    assert claimed is not None

    # Simulate the retryable-failure path: job goes to the delayed zset and
    # the claim is released after the durable requeue (worker catch-all).
    await rq.schedule_retry(str(claimed), 0.0)
    assert await rq.ack(claimed) is True
    assert await rq.promote_due() == 1

    redelivered = await rq.claim(timeout_seconds=1, lease=30)
    assert redelivered is not None
    assert str(redelivered) == job
    assert await rq.ack(redelivered) is True
