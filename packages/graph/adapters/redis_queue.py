"""Graph extraction queue - queued background extraction (T-V2-001).

Reuses the T-V1-020 durable claim/ack pattern from ingestion.adapters.redis_queue
but on separate keys so ingestion and graph jobs never collide. The choice to
use a separate queue (not reuse ingestion's queue) is deliberate:

- Ingestion jobs are generation-fenced and idempotent via stable chunk IDs
  (T-V1-016/019); graph jobs are entity/relationship upserts idempotent via
  deterministic node IDs, but have no generation fence and different retry
  semantics (graph extraction failure is terminal for that version's graph
  rows, not for the document's chunks).
- Reusing the ingestion queue would require the ingest worker to dispatch on
  job type, breaking its single-responsibility loop and risking flag-off
  parity (every ingest would need to inspect the payload to decide if it's a
  graph job).
- Separate keys keep both workers' recover_expired/promote_due isolated.

Keys are namespaced via GRAPH_QUEUE_PREFIX (default graph:<env>:) so tests
can isolate with a random prefix and inject short leases via
GRAPH_QUEUE_LEASE_SECONDS (no wall-clock sleeps in tests).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
import weakref

import redis.asyncio as redis_asyncio
from core.config import get_settings
from redis.exceptions import RedisError

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

#: T-R10: payload and queue entry are written by ONE script. Two separate
#: commands could interleave a crash between them and leave either an orphaned
#: payload nothing will ever claim, or (worse, on a retry) a queued job id whose
#: payload was never stored — a worker that pops it and finds nothing. Redis
#: runs a script atomically, so the pair is now all-or-nothing.
_ENQUEUE_LUA = """
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
redis.call('LPUSH', KEYS[2], ARGV[1])
return 1
"""

#: T-R10: durable receipt. A successful LPUSH return value is the broker
#: accepting the write, not proof the job is retrievable. This reads the job back
#: out of both structures and only then is the enqueue considered durable.
_ENQUEUE_RECEIPT_LUA = """
local payload = redis.call('HGET', KEYS[1], ARGV[1])
if not payload then return 0 end
local depth = redis.call('LPOS', KEYS[2], ARGV[1])
if not depth then return 0 end
return 1
"""

_CLAIM_LUA = """
local job = redis.call('RPOP', KEYS[1])
if not job then return false end
redis.call('ZADD', KEYS[2], ARGV[1] + ARGV[2], job)
redis.call('HSET', KEYS[3], job, ARGV[3])
return {job, ARGV[3]}
"""

_ACK_LUA = """
if redis.call('HGET', KEYS[2], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[1])
return 1
"""

_RENEW_LUA = """
if redis.call('HGET', KEYS[2], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[1])
return 1
"""

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

_CLAIM_POLL_SECONDS = 0.05
_DEFAULT_LEASE_SECONDS = 120.0


class Claim(str):
    __slots__ = ("token",)
    token: str

    def __new__(cls, job_id: str, token: str) -> Claim:
        obj = super().__new__(cls, job_id)
        obj.token = token
        return obj


_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, redis_asyncio.Redis] = (
    weakref.WeakKeyDictionary()
)


class GraphQueueUnavailable(RuntimeError):
    pass


def _prefix() -> str:
    override = os.environ.get("GRAPH_QUEUE_PREFIX")
    if override:
        return override
    return f"{get_settings().app_env}:graph:"


def queue_key() -> str:
    return f"graph:{_prefix()}queue"


def delayed_key() -> str:
    return f"graph:{_prefix()}delayed"


def claims_key() -> str:
    return f"graph:{_prefix()}claims"


def claim_tokens_key() -> str:
    return f"graph:{_prefix()}claim_tokens"


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
    return time.time()


def lease_seconds() -> float:
    raw = os.environ.get("GRAPH_QUEUE_LEASE_SECONDS")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    # Fall back to ingestion lease for test injection compatibility
    raw2 = os.environ.get("INGEST_QUEUE_LEASE_SECONDS")
    if raw2:
        try:
            value = float(raw2)
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


async def enqueue(job_payload: str) -> None:
    try:
        await get_client().lpush(queue_key(), job_payload)
    except RedisError as exc:
        raise GraphQueueUnavailable("graph queue unavailable") from exc


async def enqueue_graph_job(
    workspace_id: str,
    document_id: str,
    version_id: str,
    chunk_ids: list[str] | None = None,
) -> str:
    """Enqueue a graph extraction job for a document version.

    Returns the job id (payload) that was enqueued. No-op when GRAPH_MODE=off
    is handled by the caller - this function always enqueues.
    """
    payload = json.dumps(
        {
            "workspace_id": workspace_id,
            "document_id": document_id,
            "version_id": version_id,
            "chunk_ids": chunk_ids or [],
            # T-R11: enqueue time, so "how old is the oldest queued job" is a
            # fact rather than an inference from queue depth. Previously a deep
            # queue was indistinguishable from a young one.
            "enqueued_at": time.time(),
        }
    )
    # Use version_id as dedup key? For now each call enqueues a new job;
    # idempotency is at the store layer (upsert), so duplicate jobs are safe.
    job_id = str(uuid.uuid4())
    # Store payload under job_id for worker to fetch? Simpler: enqueue
    # payload directly. But claim expects job_id string, so enqueue job_id
    # + store payload separately. For now enqueue the JSON payload itself as
    # the job identifier (like ingestion does with job_id). To keep claim
    # string simple, enqueue job_id and store payload in hash
    # T-R10: atomic write, then a read-back receipt. A job id is only returned
    # when the payload is stored AND the id is actually in the queue, so the
    # caller can treat a returned id as durable. Previously a broker error
    # between the two writes (or a swallowed error) produced a caller that
    # believed the job was queued when nothing would ever claim it.
    try:
        client = get_client()
        await client.eval(
            _ENQUEUE_LUA,
            2,
            f"graph:{_prefix()}payloads",
            queue_key(),
            job_id,
            payload,
        )
    except RedisError as exc:
        raise GraphQueueUnavailable("graph queue unavailable") from exc
    try:
        receipt = await client.eval(
            _ENQUEUE_RECEIPT_LUA,
            2,
            f"graph:{_prefix()}payloads",
            queue_key(),
            job_id,
        )
    except RedisError as exc:
        raise GraphQueueUnavailable("graph queue unavailable") from exc
    if int(receipt or 0) != 1:
        # The write reported success but the job is not retrievable. Treat it
        # as a failure rather than handing back an id that no worker can claim.
        raise GraphQueueUnavailable(
            "graph queue did not confirm the enqueue; no durable job receipt"
        )
    return job_id


async def claim(timeout_seconds: int = 1, lease: float | None = None) -> Claim | None:
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
            raise GraphQueueUnavailable("graph queue unavailable") from exc
        if item:
            return Claim(_as_str(item[0]), _as_str(item[1]))
        if not timeout_seconds or _now() >= deadline:
            return None
        remaining = deadline - _now()
        await asyncio.sleep(min(_CLAIM_POLL_SECONDS, max(remaining, 0.0)))


async def ack(claimed: Claim) -> bool:
    try:
        released = await get_client().eval(
            _ACK_LUA, 2, claims_key(), claim_tokens_key(), str(claimed), claimed.token
        )
    except RedisError as exc:
        raise GraphQueueUnavailable("graph queue unavailable") from exc
    return bool(released)


async def renew(claimed: Claim) -> bool:
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
        raise GraphQueueUnavailable("graph queue unavailable") from exc
    return bool(extended)


async def recover_expired() -> int:
    try:
        moved = await get_client().eval(
            _RECOVER_LUA, 3, claims_key(), claim_tokens_key(), queue_key(), _now()
        )
        return int(moved)
    except RedisError as exc:
        raise GraphQueueUnavailable("graph queue unavailable") from exc


async def get_payload(job_id: str) -> dict | None:
    try:
        raw = await get_client().hget(f"graph:{_prefix()}payloads", job_id)
    except RedisError as exc:
        raise GraphQueueUnavailable("graph queue unavailable") from exc
    if raw is None:
        return None
    return json.loads(_as_str(raw))


async def delete_payload(job_id: str) -> None:
    try:
        await get_client().hdel(f"graph:{_prefix()}payloads", job_id)
    except RedisError:
        pass


async def schedule_retry(job_id: str, delay_seconds: float) -> None:
    try:
        await get_client().zadd(delayed_key(), {job_id: _now() + delay_seconds})
    except RedisError as exc:
        raise GraphQueueUnavailable("graph queue unavailable") from exc


async def promote_due() -> int:
    try:
        moved = await get_client().eval(
            _PROMOTE_DUE_LUA, 2, queue_key(), delayed_key(), _now()
        )
        return int(moved)
    except RedisError as exc:
        raise GraphQueueUnavailable("graph queue unavailable") from exc


async def queue_depth() -> int:
    try:
        return int(await get_client().llen(queue_key()))
    except RedisError as exc:
        raise GraphQueueUnavailable("graph queue unavailable") from exc


class GraphJobDisposition(str):
    SUCCESS = "success"
    RETRY = "retry"
    PARKED = "parked"
