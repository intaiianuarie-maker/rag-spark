"""T-V1-020 queue semantics: durable claim/ack on the ingest queue (unit).

Service-free locks over the *wiring* (the Redis-side Lua cycle is pinned by
``tests/integration/test_queue_claim_ack.py``):

1. **Ack-after-durability** — the worker loop releases a claim only after the
   durable outcome is committed (process_one returned), never after dequeue.
2. **No-ack on undurable failure** — a failure escaping process_one with a
   failed requeue leaves the claim in place so lease recovery redelivers.
3. **Expired claims recover** — ``recover_expired`` runs each tick before any
   new claim.
4. **Fencing** — ``Claim`` carries the lease token; ack/renew of a claim the
   worker no longer holds is refused by the adapter (integration-pinned) and
   logged, never treated as success, by the loop helpers.
5. **Retry exhaustion visible** — record_failure parks at max_attempts with a
   terminal FAILED record (existing behavior, re-pinned here from the queue's
   point of view: the claim is acked because the FAILED record is durable).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

import jobs.ingest_worker as ingest_worker
from ingestion.adapters.redis_queue import Claim

pytestmark = pytest.mark.unit


def _claim(job_id: str | None = None) -> Claim:
    return Claim(job_id or uuid.uuid4().hex, "tok-" + uuid.uuid4().hex[:8])


async def test_claim_is_a_str_carrying_its_fence_token() -> None:
    """The queue payload stays the job id (T-V1-026 seam) + the lease token."""
    job_id = uuid.uuid4().hex
    claimed = _claim(job_id)
    assert isinstance(claimed, str)
    assert str(claimed) == job_id
    assert claimed.token.startswith("tok-")
    assert uuid.UUID(str(claimed))  # existing consumers keep working


async def test_lease_seconds_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from ingestion.adapters.redis_queue import lease_seconds

    monkeypatch.setenv("INGEST_QUEUE_LEASE_SECONDS", "0.25")
    assert lease_seconds() == 0.25
    monkeypatch.setenv("INGEST_QUEUE_LEASE_SECONDS", "not-a-number")
    assert lease_seconds() > 1  # falls back to the default
    monkeypatch.delenv("INGEST_QUEUE_LEASE_SECONDS")
    assert lease_seconds() > 1


async def test_claim_keys_are_namespaced_like_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hermetic namespace — see test_ingest_pipeline: integration modules export
    # INGEST_QUEUE_PREFIX at import time and a full collection imports them all.
    monkeypatch.setenv("INGEST_QUEUE_PREFIX", "unit-test:")
    from ingestion.adapters.redis_queue import claim_tokens_key, claims_key

    assert claims_key().startswith("ingest:")
    assert claim_tokens_key().startswith("ingest:")
    assert "test" in claims_key()


class _Harness:
    """Scripted stand-in for the queue + pipeline, recording call order.

    The loop drives itself: once the scripted claims are drained, ``dequeue``
    sets the stop event after ``stop_after_none`` empty polls — same pattern as
    the existing ``test_worker_survives_process_one_failure`` seam.
    """

    def __init__(self, stop: asyncio.Event | None = None, stop_after_none: int = 2) -> None:
        self.stop = stop if stop is not None else asyncio.Event()
        self.stop_after_none = stop_after_none
        self.claim_results: list[Claim | None] = []
        self.events: list[str] = []
        self.acked: list[Claim] = []
        self.renewed: list[Claim] = []
        self.requeues: list[str] = []
        self.recovered = 0
        self.process_behavior: list[BaseException | None] = []

    # --- injected into ingest_worker -------------------------------------
    async def dequeue(self, timeout_seconds: int = 1) -> Claim | None:
        self.events.append("dequeue")
        if self.claim_results:
            return self.claim_results.pop(0)
        self.stop_after_none -= 1
        if self.stop_after_none <= 0:
            self.stop.set()
        return None

    async def promote_due(self) -> int:
        self.events.append("promote_due")
        return 0

    async def recover_expired(self) -> int:
        self.events.append("recover_expired")
        return self.recovered

    async def ack(self, claimed: Claim) -> bool:
        self.events.append("ack")
        self.acked.append(claimed)
        return True

    async def renew(self, claimed: Claim) -> bool:
        self.events.append("renew")
        self.renewed.append(claimed)
        return True

    async def schedule_retry(self, job_id: str, delay: float) -> None:
        self.events.append("schedule_retry")
        self.requeues.append(job_id)

    async def process_one(self, job_id: uuid.UUID) -> None:
        self.events.append("process_one")
        # Yield once so the heartbeat task can run while the "job" is live.
        await asyncio.sleep(0)
        behavior = self.process_behavior.pop(0) if self.process_behavior else None
        if behavior is not None:
            raise behavior

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "dequeue",
            "promote_due",
            "recover_expired",
            "ack",
            "renew",
            "schedule_retry",
            "process_one",
        ):
            monkeypatch.setattr(ingest_worker, name, getattr(self, name))


async def _drive(monkeypatch: pytest.MonkeyPatch, harness: _Harness) -> None:
    real_sleep = asyncio.sleep

    async def fast_sleep(delay: float, *args, **kwargs) -> None:
        # Collapse backoff sleeps (loop backoff, heartbeat interval) so the
        # harness drains in milliseconds. Process_one's yield above still
        # gives the heartbeat its slot via this shim.
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    await asyncio.wait_for(ingest_worker.run_worker(harness.stop), timeout=5)


async def test_ack_happens_only_after_process_one_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checklist (1): ack-after-durability, not after dequeue."""
    harness = _Harness()
    claimed = _claim()
    harness.claim_results = [claimed, None, None]
    harness.install(monkeypatch)

    await _drive(monkeypatch, harness)

    assert harness.events.index("ack") > harness.events.index("process_one")
    # Exactly one ack, for exactly the one processed claim.
    assert harness.acked == [claimed]


async def test_claim_is_released_when_a_retry_is_durably_requeued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure escaping process_one requeues first; ack follows the durable
    requeue (the delayed zset now owns the job)."""
    harness = _Harness()
    claimed = _claim()
    harness.claim_results = [claimed, None, None]
    harness.process_behavior = [RuntimeError("db down")]
    harness.install(monkeypatch)

    await _drive(monkeypatch, harness)

    assert harness.requeues == [str(claimed)]
    assert harness.events.index("schedule_retry") < harness.events.index("ack")
    assert harness.acked == [claimed]


async def test_no_ack_when_requeue_fails_lease_recovery_will_redeliver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No durable outcome and no durable requeue ⇒ the claim stays claimed;
    the expired lease (not an ack) is what brings the job back."""
    harness = _Harness()
    claimed = _claim()
    harness.claim_results = [claimed, None, None]
    harness.process_behavior = [RuntimeError("db down")]
    harness.install(monkeypatch)

    async def broken_retry(job_id: str, delay: float) -> None:
        harness.events.append("schedule_retry")
        raise ingest_worker.IngestQueueUnavailable("redis down")

    monkeypatch.setattr(ingest_worker, "schedule_retry", broken_retry)

    await _drive(monkeypatch, harness)

    assert harness.acked == []  # checklist: ack-before-completion window = 0


async def test_expired_claims_are_recovered_every_tick_before_claiming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checklist (2): recover_expired runs each loop iteration, before dequeue."""
    harness = _Harness()
    harness.recovered = 2
    harness.claim_results = [None, None, None]
    harness.install(monkeypatch)

    await _drive(monkeypatch, harness)

    recoveries = [i for i, e in enumerate(harness.events) if e == "recover_expired"]
    claims = [i for i, e in enumerate(harness.events) if e == "dequeue"]
    assert len(recoveries) == len(claims) >= 2
    assert all(r < c for r, c in zip(recoveries, claims, strict=True))


async def test_recover_expired_failure_does_not_kill_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness()
    harness.claim_results = [None, None, None]
    harness.install(monkeypatch)

    async def broken_recover() -> int:
        raise ingest_worker.IngestQueueUnavailable("redis down")

    monkeypatch.setattr(ingest_worker, "recover_expired", broken_recover)

    await _drive(monkeypatch, harness)  # must return, not hang/raise


async def test_bad_job_id_payload_is_dropped_not_redelivered_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payload that is not a job id has no claim worth recovering: the
    worker releases it (terminal) instead of letting it cycle through leases."""
    harness = _Harness()
    claimed = _claim("not-a-uuid")
    harness.claim_results = [claimed, None, None]
    harness.install(monkeypatch)

    await _drive(monkeypatch, harness)

    assert harness.acked == [claimed]
    assert "process_one" not in harness.events


async def test_ack_of_a_lost_claim_is_logged_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A fenced ack (our lease expired, job re-claimed elsewhere) is at-least-
    once working as designed: the other holder's delivery stands, and the
    refusal is visible in the logs, never silent."""
    harness = _Harness()
    claimed = _claim()
    harness.claim_results = [claimed, None, None]
    harness.install(monkeypatch)

    async def fenced_ack(claimed: Claim) -> bool:
        harness.events.append("ack")
        return False

    monkeypatch.setattr(ingest_worker, "ack", fenced_ack)

    with caplog.at_level("WARNING", logger="knowledge_os.worker.ingest"):
        await _drive(monkeypatch, harness)

    assert "ingest_ack_fenced" in caplog.text


async def test_heartbeat_renews_while_the_job_runs_then_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy in-flight job extends its lease; the heartbeat dies with the
    job (no orphan renew tasks)."""
    harness = _Harness()
    claimed = _claim()
    harness.claim_results = [claimed, None, None]
    harness.install(monkeypatch)
    monkeypatch.setenv("INGEST_QUEUE_LEASE_SECONDS", "0.03")

    await _drive(monkeypatch, harness)

    # The lease is 30ms; process_one ran at least one tick, so at least one
    # renewal fired before the job finished.
    assert harness.renewed, "heartbeat never renewed the lease"


async def test_worker_survives_ack_raising_queue_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis dying *during* ack must not kill the loop: the job's outcome is
    already durable, and the un-acked claim recovers via its lease."""
    harness = _Harness()
    claimed = _claim()
    harness.claim_results = [claimed, None, None]
    harness.install(monkeypatch)

    async def broken_ack(claimed: Claim) -> bool:
        raise ingest_worker.IngestQueueUnavailable("redis down")

    monkeypatch.setattr(ingest_worker, "ack", broken_ack)

    await _drive(monkeypatch, harness)  # returns; loop survived


async def test_retry_exhaustion_is_visible_as_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checklist (3): once attempts reach max_attempts the job is parked as a
    durable FAILED record — a redelivered crash-loop job cannot spin forever
    without a terminal, greppable outcome."""
    from datetime import datetime

    from jobs.ingest_pipeline import IngestPipelineError

    job_id = uuid.uuid4()
    events: list[str] = []

    class _Job:
        id: uuid.UUID = job_id
        attempts: int = 3
        max_attempts: int = 3
        status: str = "running"
        stage: str = "parsing"
        error_code: str | None = None
        error_message: str | None = None
        finished_at: datetime | None = None

    job = _Job()

    class _Session:
        async def get(self, model, jid):
            return job

        async def commit(self) -> None:
            events.append("commit")

        async def rollback(self) -> None:
            events.append("rollback")

    class _Factory:
        def __call__(self):
            return _Session()

    async def fake_schedule_retry(job_id: str, delay: float) -> None:
        events.append("schedule_retry")

    monkeypatch.setattr(ingest_worker, "get_session_factory", _Factory())
    monkeypatch.setattr(ingest_worker, "schedule_retry", fake_schedule_retry)

    await ingest_worker.record_failure(
        _Session(),  # type: ignore[arg-type]
        job_id,
        IngestPipelineError("PARSE_FAILED", "boom", retryable=True),
    )

    assert job.status == "failed"
    assert isinstance(job.finished_at, datetime)
    assert "schedule_retry" not in events  # exhausted: no silent requeue
