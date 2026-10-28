"""T-IMP-002 concurrency tests for SqliteAgentRunQueue claim() atomicity.

Verifies that concurrent claims across separate database connections never
grant the same run to multiple consumers, even under a barrier-synchronized
race. Uses raw threads with asyncio.run to simulate concurrent workers.
"""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from packages.stores.agent_stores import SqliteAgentRunQueue


def test_concurrent_claims_distinct(tmp_path) -> None:
    """Two workers claim concurrently — exactly one gets the run, the other None."""
    db = tmp_path / "q.db"
    q1 = SqliteAgentRunQueue(db)
    q2 = SqliteAgentRunQueue(db)
    asyncio.run(q1.enqueue("run-1"))
    assert asyncio.run(q1.depth()) == 1

    barrier = threading.Barrier(2)
    results: dict[int, object] = {}
    errors: list[Exception] = []

    def worker(idx: int, queue: SqliteAgentRunQueue) -> None:
        try:
            barrier.wait(timeout=10)
            results[idx] = asyncio.run(queue.claim(30.0))
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(worker, 0, q1),
            pool.submit(worker, 1, q2),
        ]
        for f in futures:
            f.result(timeout=15)

    assert not errors, f"errors during concurrent claim: {errors}"
    claim_0 = results[0]
    claim_1 = results[1]
    claims_made = [c for c in (claim_0, claim_1) if c is not None]
    assert len(claims_made) == 1, (
        f"Both workers got a claim — race condition! "
        f"claim_0={claim_0}, claim_1={claim_1}"
    )
    assert str(claims_made[0]) == "run-1"
    nulls = [c for c in (claim_0, claim_1) if c is None]
    assert len(nulls) == 1


def test_concurrent_claims_many_workers(tmp_path) -> None:
    """10 workers, 5 runs — exactly 5 claims, no duplicates, no losses."""
    db = tmp_path / "q.db"
    q = SqliteAgentRunQueue(db)

    async def setup() -> None:
        for i in range(5):
            await q.enqueue(f"run-{i}")

    asyncio.run(setup())
    assert asyncio.run(q.depth()) == 5

    queues = [SqliteAgentRunQueue(db) for _ in range(10)]
    barrier = threading.Barrier(10)
    all_claims: list = []
    claim_lock = threading.Lock()
    errors: list[Exception] = []

    def worker(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            claim = asyncio.run(queues[idx].claim(30.0))
            with claim_lock:
                all_claims.append(claim)
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(worker, i) for i in range(10)]
        for f in futures:
            f.result(timeout=20)

    assert not errors, f"errors during concurrent claims: {errors}"
    claimed_runs = [str(c) for c in all_claims if c is not None]
    claimed_tokens = [c.token for c in all_claims if c is not None]

    assert len(claimed_runs) == 5, (
        f"Expected 5 claims, got {len(claimed_runs)}. all_claims={all_claims}"
    )
    assert len(set(claimed_runs)) == 5
    assert len(set(claimed_tokens)) == 5
    assert set(claimed_runs) == {"run-0", "run-1", "run-2", "run-3", "run-4"}
    assert asyncio.run(q.depth()) == 0


def test_concurrent_claims_single_worker_gets_all(tmp_path) -> None:
    """3 workers, 1 run — exactly one succeeds, others get None."""
    db = tmp_path / "q.db"
    q1 = SqliteAgentRunQueue(db)
    asyncio.run(q1.enqueue("solo-run"))

    barrier = threading.Barrier(3)
    results: list = [None, None, None]
    errors: list[Exception] = []
    queues = [SqliteAgentRunQueue(db) for _ in range(3)]

    def worker(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[idx] = asyncio.run(queues[idx].claim(30.0))
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(worker, i) for i in range(3)]
        for f in futures:
            f.result(timeout=15)

    assert not errors
    claims = [c for c in results if c is not None]
    assert len(claims) == 1, f"Expected exactly 1 claim, got {len(claims)}: {results}"
    assert str(claims[0]) == "solo-run"


def test_claim_after_expired_recover(tmp_path) -> None:
    """A recovered run from recover_expired can be claimed by another worker."""
    db = tmp_path / "q.db"
    q1 = SqliteAgentRunQueue(db)
    asyncio.run(q1.enqueue("expired-run"))

    claim1 = asyncio.run(q1.claim(lease_seconds=0.01))
    assert claim1 is not None
    assert str(claim1) == "expired-run"

    # Immediately, another worker should not get it (still claimed)
    claim2 = asyncio.run(SqliteAgentRunQueue(db).claim(30.0))
    assert claim2 is None

    time.sleep(0.05)

    recovered = asyncio.run(q1.recover_expired())
    assert recovered == 1
    assert asyncio.run(q1.depth()) == 1

    claim3 = asyncio.run(SqliteAgentRunQueue(db).claim(30.0))
    assert claim3 is not None
    assert str(claim3) == "expired-run"
    assert claim3.token != claim1.token


def test_double_claim_same_worker_returns_none(tmp_path) -> None:
    """Claim is destructive — a second immediate claim returns None."""
    db = tmp_path / "q.db"
    q = SqliteAgentRunQueue(db)
    asyncio.run(q.enqueue("once-run"))
    claim1 = asyncio.run(q.claim(30.0))
    assert claim1 is not None
    claim2 = asyncio.run(q.claim(30.0))
    assert claim2 is None
    assert asyncio.run(q.depth()) == 0
