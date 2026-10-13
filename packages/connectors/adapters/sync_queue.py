"""Redis adapter for the T-V1-034 sync queue — the T-V1-020 claim/ack protocol.

Vendor Redis access lives here (``/adapters/`` is the CI vendor fence);
``connectors.sync_consumer`` only sees the ``SyncQueue`` protocol.

The lease protocol is **reused, not reimplemented**: the same atomic
claim/ack/recover Lua scripts that gate ingestion jobs (imported from
``ingestion.adapters.redis_queue`` — a contract test pins their identity, so a
rename there fails loudly here) run against a **separate key namespace**
(``sync:<env>:queue|claims|claim_tokens``). Separate namespace, deliberately:
an ingestion worker claiming ``sync:ws-a/docs`` would choke on it (it expects a
job UUID), and a sync consumer claiming an ingest job would swallow a document
upload. Same broker, disjoint queues.

``SYNC_QUEUE_PREFIX`` (or the shared ``INGEST_QUEUE_PREFIX``) namespaces the
keys so tests never collide with a live worker.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from core.config import get_settings
from ingestion.adapters.redis_queue import (
    _ACK_LUA,
    _CLAIM_LUA,
    _CLAIM_POLL_SECONDS,
    _RECOVER_LUA,
    _as_str,
    _now,
    get_client,
    lease_seconds,
)
from redis.exceptions import RedisError

from ..sync_consumer import SyncClaim, encode_sync_job

#: Job payload → lease deadline zset, per namespace.
_QUEUE = "queue"
_CLAIMS = "claims"
_TOKENS = "claim_tokens"


class SyncQueueUnavailable(RuntimeError):
    """Redis (the sync queue) cannot be reached."""


def _prefix() -> str:
    override = os.environ.get("SYNC_QUEUE_PREFIX") or os.environ.get("INGEST_QUEUE_PREFIX")
    if override:
        return override
    return f"{get_settings().app_env}:"


def queue_key() -> str:
    return f"sync:{_prefix()}{_QUEUE}"


def claims_key() -> str:
    return f"sync:{_prefix()}{_CLAIMS}"


def claim_tokens_key() -> str:
    return f"sync:{_prefix()}{_TOKENS}"


class RedisSyncQueue:
    """``SyncQueue`` over Redis using the T-V1-020 lease scripts."""

    async def claim(self, timeout_seconds: int = 1) -> SyncClaim | None:
        """Atomically claim the next sync job under a lease.

        Blocks up to ``timeout_seconds`` (a Lua script cannot block, so this
        is the same short poll loop the ingestion queue uses). Returns
        ``None`` on timeout.
        """
        deadline = _now() + timeout_seconds if timeout_seconds else 0.0
        claim_lease = lease_seconds()
        while True:
            token = uuid.uuid4().hex
            try:
                item = await get_client().eval(
                    _CLAIM_LUA,
                    3,
                    queue_key(),
                    claims_key(),
                    claim_tokens_key(),
                    _now(),
                    claim_lease,
                    token,
                )
            except RedisError as exc:
                raise SyncQueueUnavailable("sync queue unavailable") from exc
            if item:
                return SyncClaim(job_id=_as_str(item[0]), token=_as_str(item[1]))
            if not timeout_seconds or _now() >= deadline:
                return None
            remaining = deadline - _now()
            await asyncio.sleep(min(_CLAIM_POLL_SECONDS, max(remaining, 0.0)))

    async def ack(self, claim: SyncClaim) -> bool:
        """Release the claim **after** the durable outcome is committed.

        Token-fenced exactly like the ingestion queue: a holder whose lease
        already expired (job re-claimed elsewhere) gets ``False`` and cannot
        touch the live claim.
        """
        try:
            released = await get_client().eval(
                _ACK_LUA, 2, claims_key(), claim_tokens_key(), claim.job_id, claim.token
            )
        except RedisError as exc:
            raise SyncQueueUnavailable("sync queue unavailable") from exc
        return bool(released)


async def recover_expired() -> int:
    """Return every expired sync claim to the ready queue (T-V1-020 recovery).

    A consumer that died mid-run leaves its claim un-acked on purpose; this is
    what redelivers it, and the persisted keyset cursor is where the retry
    picks up.
    """
    try:
        moved = await get_client().eval(
            _RECOVER_LUA, 3, claims_key(), claim_tokens_key(), queue_key(), _now()
        )
        return int(moved)
    except RedisError as exc:
        raise SyncQueueUnavailable("sync queue unavailable") from exc


async def enqueue_sync_job(workspace_id: str, connector_id: str | None = None) -> str:
    """Queue a sync job for a workspace (optionally one connector).

    Returns the payload it enqueued so callers can log/track the job id.
    """
    payload = encode_sync_job(workspace_id, connector_id)
    try:
        await get_client().lpush(queue_key(), payload)
    except RedisError as exc:
        raise SyncQueueUnavailable("sync queue unavailable") from exc
    return payload
