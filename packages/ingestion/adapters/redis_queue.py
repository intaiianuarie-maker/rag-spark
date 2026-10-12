"""Redis ingestion queue adapter (M2-001c, T-V1-020 durable claim/ack).

Vendor Redis access lives here (``/adapters/`` is the CI vendor fence); the
public ``ingestion.queue`` module re-exports this typed surface.

Keys are **namespaced** (``ingest:<env>:queue``) so tests cannot collide with a
live worker on the shared Redis. ``INGEST_QUEUE_PREFIX`` overrides the prefix.

T-V1-020 (claim/lease): a worker no longer *destructively* pops a job id and
hopes to survive. :func:`claim` atomically moves a job from the ready list
onto a **claims** zset scored by lease expiry and mints a per-claim **token**
(a lease fence). :func:`ack` removes the claim **only** when the caller still
holds the current token — a zombie worker whose lease expired and whose job was
re-claimed elsewhere can no longer ack (or drop) the new claim. Expired claims
are redelivered by :func:`recover_expired` (at-least-once; publication is
idempotent via the T-V1-016 stable IDs, so redelivery is safe).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
import weakref

import redis.asyncio as redis_asyncio
from core.config import get_settings
from redis.exceptions import RedisError

#: Atomic promote: move due delayed jobs to the ready queue without a
#: zrem→lpush crash window (KEYS[1]=queue, KEYS[2]=delayed; ARGV[1]=now).
_PROMOTE_DUE_LUA = """
local due = redis.call('ZRANGEBYSCORE', KEYS[2], 0, ARGV[1])
local moved = 0
for _, job in ipairs(due) do
  if redis.call('ZREM', KEYS[2], job) == 1 then
    redis.call('LPUSH', KEYS[1], job)
    moved = moved + 1
  end
end
return moved
"""

#: Atomic claim: pop one job from the ready list, register it on the claims
#: zset with a lease deadline, and mint a claim token — all in one script, so
#: there is no window where a job is neither queued nor claimed
#: (KEYS[1]=queue, KEYS[2]=claims, KEYS[3]=claim_tokens;
#: ARGV[1]=now, ARGV[2]=lease_seconds, ARGV[3]=token).
_CLAIM_LUA = """
local job = redis.call('RPOP', KEYS[1])
if not job then return false end
redis.call('ZADD', KEYS[2], ARGV[1] + ARGV[2], job)
redis.call('HSET', KEYS[3], job, ARGV[3])
return {job, ARGV[3]}
"""

#: Token-fenced ack: drop the claim only if the caller still holds the current
#: claim token. A stale holder (lease expired, job re-claimed) gets 0 and the
#: live claim is untouched (KEYS[1]=claims, KEYS[2]=claim_tokens;
#: ARGV[1]=job_id, ARGV[2]=token).
_ACK_LUA = """
if redis.call('HGET', KEYS[2], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[1])
return 1
"""

#: Token-fenced lease renewal (same keys as _ACK_LUA; ARGV[3]=new deadline).
_RENEW_LUA = """
if redis.call('HGET', KEYS[2], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[1])
return 1
"""

#: Atomic recovery: every claim whose lease deadline has passed is removed
#: (claim + token) and pushed back onto the tail of the ready list so it is
#: the next job claimed. At-least-once: the dead worker's in-flight copy (if
#: any) still finishes; publication idempotence (T-V1-016) makes that safe.
#: (KEYS[1]=claims, KEYS[2]=claim_tokens, KEYS[3]=queue; ARGV[1]=now).
_RECOVER_LUA = """
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local n = 0
for _, job in ipairs(expired) do
  redis.call('ZREM', KEYS[1], job)
  redis.call('HDEL', KEYS[2], job)
  redis.call('RPUSH', KEYS[3], job)
  n = n + 1
end
return n
"""

#: How often an empty claim poll retries inside its blocking window
#: (BRPOP cannot run inside a Lua script, so the blocking wait is a short
#: poll loop around the atomic claim script).
_CLAIM_POLL_SECONDS = 0.05

#: Default claim lease: long enough for a normal ingest (parse→embed→index),
#: overridden by ``INGEST_QUEUE_LEASE_SECONDS`` (tests use sub-second values).
_DEFAULT_LEASE_SECONDS = 120.0


class Claim(str):
    """A claimed job: the queue payload **is** the job id (T-V1-026), plus the
    lease fence token minted by :func:`claim`.

    Subclassing ``str`` keeps every existing consumer (``uuid.UUID(...)``,
    logging, ``schedule_retry``) working unchanged while carrying the token
    that :func:`ack`/:func:`renew` verify — a stale holder (lease expired,
    job re-claimed elsewhere) fails the token check and cannot touch the new
    claim.
    """

    __slots__ = ("token",)

    token: str

    def __new__(cls, job_id: str, token: str) -> Claim:
        obj = super().__new__(cls, job_id)
        obj.token = token
        return obj


_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, redis_asyncio.Redis] = (
    weakref.WeakKeyDictionary()
)


class IngestQueueUnavailable(RuntimeError):
    """Redis (the job queue) cannot be reached."""


def _prefix() -> str:
    override = os.environ.get("INGEST_QUEUE_PREFIX")
    if override:
        return override
    return f"{get_settings().app_env}:"


def queue_key() -> str:
    return f"ingest:{_prefix()}queue"


def delayed_key() -> str:
    return f"ingest:{_prefix()}delayed"


def claims_key() -> str:
    """ZSET: claimed job_id → lease deadline (epoch seconds)."""
    return f"ingest:{_prefix()}claims"


def claim_tokens_key() -> str:
    """HASH: job_id → the current claim's fence token (one live claim per job)."""
    return f"ingest:{_prefix()}claim_tokens"


def get_client() -> redis_asyncio.Redis:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        settings = get_settings()
        client = redis_asyncio.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2
        )
        _clients[loop] = client
    return client


def _as_str(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _now() -> float:
    """Clock seam (T-V1-020): tests freeze time here instead of sleeping."""
    return time.time()


def lease_seconds() -> float:
    """Claim lease duration; ``INGEST_QUEUE_LEASE_SECONDS`` overrides the
    default (tests use sub-second values to exercise expiry quickly)."""
    raw = os.environ.get("INGEST_QUEUE_LEASE_SECONDS")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return _DEFAULT_LEASE_SECONDS


async def close_client() -> None:
    loop = asyncio.get_running_loop()
    client = _clients.pop(loop, None)
    if client is not None:
        await client.aclose()


async def enqueue(job_id: str) -> None:
    try:
        await get_client().lpush(queue_key(), job_id)
    except RedisError as exc:
        raise IngestQueueUnavailable("ingestion queue unavailable") from exc


async def claim(timeout_seconds: int = 1, lease: float | None = None) -> Claim | None:
    """Atomically claim the next queued job under a lease (T-V1-020).

    One Lua script RPOPs the job **and** registers it on the claims zset with
    a lease deadline and a freshly minted claim token, so there is no window
    in which a job is neither queued nor claimed (the old BRPOP popped the job
    out of Redis entirely — a worker dying mid-job took it to the grave).

    Blocks up to ``timeout_seconds`` polling for work (a Lua script cannot
    block). Returns ``None`` on timeout. The caller must :func:`ack` only
    after a durable outcome is recorded.
    """
    deadline = _now() + timeout_seconds if timeout_seconds else 0.0
    claim_lease = lease if lease is not None else lease_seconds()
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
            raise IngestQueueUnavailable("ingestion queue unavailable") from exc
        if item:
            return Claim(_as_str(item[0]), _as_str(item[1]))
        if not timeout_seconds or _now() >= deadline:
            return None
        remaining = deadline - _now()
        await asyncio.sleep(min(_CLAIM_POLL_SECONDS, max(remaining, 0.0)))


async def ack(claimed: Claim) -> bool:
    """Release a claim **after** its durable outcome is committed (T-V1-020).

    Token-fenced: returns ``False`` (claim untouched) when this holder's
    lease already expired and the job was re-claimed elsewhere — a zombie
    worker can never drop a live claim. Returning ``False`` is at-least-once
    working as designed: the other holder's delivery stands.
    """
    try:
        released = await get_client().eval(
            _ACK_LUA, 2, claims_key(), claim_tokens_key(), str(claimed), claimed.token
        )
    except RedisError as exc:
        raise IngestQueueUnavailable("ingestion queue unavailable") from exc
    return bool(released)


async def renew(claimed: Claim) -> bool:
    """Extend the lease on a healthy in-flight claim (worker heartbeat).

    Token-fenced like :func:`ack`: returns ``False`` once the claim is no
    longer ours (expired + re-claimed), so the caller stops heartbeating.
    """
    try:
        extended = await get_client().eval(
            _RENEW_LUA,
            2,
            claims_key(),
            claim_tokens_key(),
            str(claimed),
            claimed.token,
            _now() + lease_seconds(),
        )
    except RedisError as exc:
        raise IngestQueueUnavailable("ingestion queue unavailable") from exc
    return bool(extended)


async def recover_expired() -> int:
    """Return every expired claim to the ready queue (T-V1-020 recovery).

    A dead worker's claim passes its lease deadline here and is re-queued at
    the head of the line; its token is destroyed, so if the "dead" worker is
    in fact still running, its later ack/renew fails the fence instead of
    corrupting the new claim. Atomic in one script — no zrem→lpush window.
    """
    try:
        moved = await get_client().eval(
            _RECOVER_LUA, 3, claims_key(), claim_tokens_key(), queue_key(), _now()
        )
        return int(moved)
    except RedisError as exc:
        raise IngestQueueUnavailable("ingestion queue unavailable") from exc


async def dequeue(timeout_seconds: int = 1) -> str | None:
    """Legacy destructive pop — superseded by :func:`claim` (T-V1-020).

    Kept only so ``ingestion.queue``'s re-export surface stays importable;
    the worker loop now claims with a lease instead of popping blindly.
    """
    try:
        item = await get_client().brpop(queue_key(), timeout=timeout_seconds)
    except RedisError as exc:
        raise IngestQueueUnavailable("ingestion queue unavailable") from exc
    if item is None:
        return None
    return _as_str(item[1])


async def schedule_retry(job_id: str, delay_seconds: float) -> None:
    try:
        await get_client().zadd(delayed_key(), {job_id: _now() + delay_seconds})
    except RedisError as exc:
        raise IngestQueueUnavailable("ingestion queue unavailable") from exc


async def promote_due() -> int:
    """Atomically move due delayed jobs back onto the ready queue."""
    try:
        moved = await get_client().eval(
            _PROMOTE_DUE_LUA, 2, queue_key(), delayed_key(), _now()
        )
        return int(moved)
    except RedisError as exc:
        raise IngestQueueUnavailable("ingestion queue unavailable") from exc


async def queue_depth() -> int:
    try:
        return int(await get_client().llen(queue_key()))
    except RedisError as exc:
        raise IngestQueueUnavailable("ingestion queue unavailable") from exc
