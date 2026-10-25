"""Supervised graph consumer and queue observability (T-R11, Phase 2).

T-R10 made the graph queue reliable: a durable enqueue receipt, atomic writes,
and a propagated typed failure. This module adds the other half — something
that actually CONSUMES those jobs — plus the supervision and the observables
that make an idle or wedged consumer visible instead of silent.

Three things live here:

1. :func:`run_supervised_graph_consumer` — runs the extraction loop under a
   restart policy. The graph worker is optional infrastructure, so a crash must
   not take the whole worker process down and must not, equally, be invisible:
   each restart is logged with a bounded backoff and a consecutive-failure count.

2. :func:`collect_queue_observables` — depth, delayed count, live claims, the
   OLDEST queued job age, and whether a consumer heartbeat has gone stale. A
   queue that is deep and growing with no consumer is the failure mode nobody
   notices, because every individual enqueue succeeded.

3. The heartbeat itself — a consumer advertises liveness into Redis so "is
   anyone actually consuming?" is answerable from outside the process.

Deliberately NOT here: any change to R10's receipt semantics. A job is consumed
only if the enqueue produced a confirmed receipt, and claiming uses the existing
atomic claim script. This module supervises and reports; it does not redefine
what a valid job is.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

from redis.exceptions import RedisError

logger = logging.getLogger("knowledge_os.graph.consumer")

#: A consumer is considered STALE when its heartbeat is older than this. Chosen
#: well above the poll interval and above one restart backoff step, so a normal
#: restart never reads as a dead consumer.
DEFAULT_HEARTBEAT_STALE_SECONDS = 30.0

#: Restart policy: backoff between attempts after a consumer crash, and the
#: point past which the supervisor gives up rather than hot-looping forever.
DEFAULT_RESTART_BACKOFF_SECONDS = 2.0
DEFAULT_MAX_BACKOFF_SECONDS = 30.0
DEFAULT_MAX_CONSECUTIVE_FAILURES = 10

#: The queue contract this consumer honours (T-R10): a job is only processed if
#: its payload exists. Mirrors handle_graph_job's guard but raises earlier and
#: more cheaply, so an orphan is reported as an orphan rather than as a
#: mysterious extraction failure.
JOB_PAYLOAD_REQUIRED = "graph job payload is absent; T-R10 receipt was not durable"


def graph_mode() -> str:
    """GRAPH_MODE, normalised. Default off keeps the flag-off path inert."""
    return os.environ.get("GRAPH_MODE", "off").strip().lower()


def consumer_enabled(mode: str | None = None) -> bool:
    """Only run a consumer when extraction is actually switched on."""
    return (mode or graph_mode()) in ("rule", "llm")


def heartbeat_key() -> str:
    from graph.queue import _prefix

    return f"graph:{_prefix()}consumer_heartbeat"


def _now() -> float:
    override = os.environ.get("GRAPH_CLOCK")
    if override:
        return float(override)
    return time.time()


async def publish_heartbeat(*, consumer_id: str, state: str) -> None:
    """Advertise consumer liveness. Failure is logged, never fatal.

    A consumer that cannot write its heartbeat must keep consuming — losing the
    queue to a metrics write would be a worse outage than a missing metric.
    """
    from graph.queue import get_client

    try:
        await get_client().hset(
            heartbeat_key(),
            consumer_id,
            f"{_now():.3f}:{state}",
        )
    except RedisError as exc:
        logger.warning(
            "graph_consumer_heartbeat_failed",
            extra={
                "event": "graph_consumer_heartbeat_failed",
                "consumer_id": consumer_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


# --------------------------------------------------------------------------
# observables
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueueObservables:
    """A point-in-time view of the graph queue, safe to log or expose.

    ``oldest_age_seconds`` is None when the queue is empty. ``consumer_count``
    and ``stale_consumer_count`` are what turn "the queue is deep" into "the
    queue is deep AND nobody is consuming it".
    """

    queue_depth: int
    delayed_count: int
    live_claims: int
    oldest_age_seconds: float | None
    consumer_count: int
    stale_consumer_count: int
    max_consumer_age_seconds: float | None
    reachable: bool = True
    error: str = ""

    @property
    def is_stalled(self) -> bool:
        """Work is waiting, unclaimed, and no fresh consumer is beating.

        The single condition worth alerting on. A deep queue with a live
        consumer is normal; a deep queue with none is an outage.
        """
        return (
            self.reachable
            and self.queue_depth > 0
            and self.stale_consumer_count >= self.consumer_count
            and self.consumer_count >= 0
        )

    def as_extra(self) -> dict[str, object]:
        return {
            "queue_depth": self.queue_depth,
            "delayed_count": self.delayed_count,
            "live_claims": self.live_claims,
            "oldest_age_seconds": (
                None if self.oldest_age_seconds is None
                else round(self.oldest_age_seconds, 3)
            ),
            "consumer_count": self.consumer_count,
            "stale_consumer_count": self.stale_consumer_count,
            "stalled": self.is_stalled,
        }


async def collect_queue_observables(
    *,
    stale_after: float = DEFAULT_HEARTBEAT_STALE_SECONDS,
) -> QueueObservables:
    """Read queue depth, oldest job age and consumer liveness.

    Never raises: a broker outage yields ``reachable=False`` plus the error, so
    a monitoring call cannot itself become the outage. Each read is one pipeline
    so the numbers are consistent with each other.
    """
    from graph.queue import claims_key, delayed_key, get_client, queue_key

    client = get_client()
    try:
        results = await client.eval(
            _OBSERVABLES_LUA,
            5,
            queue_key(),
            delayed_key(),
            claims_key(),
            heartbeat_key(),
            _prefixed_payloads(),
            f"{_now():.3f}",
            f"{stale_after:.3f}",
        )
    except RedisError as exc:
        return QueueObservables(
            queue_depth=0,
            delayed_count=0,
            live_claims=0,
            oldest_age_seconds=None,
            consumer_count=0,
            stale_consumer_count=0,
            max_consumer_age_seconds=None,
            reachable=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    depth, delayed, claims, oldest, consumers, stale, oldest_consumer = (
        int(v) for v in results
    )
    return QueueObservables(
        queue_depth=depth,
        delayed_count=delayed,
        live_claims=claims,
        oldest_age_seconds=None if oldest < 0 else float(oldest),
        consumer_count=consumers,
        stale_consumer_count=stale,
        max_consumer_age_seconds=(
            None if oldest_consumer < 0 else float(oldest_consumer)
        ),
    )


def _prefixed_payloads() -> str:
    from graph.queue import _prefix

    return f"graph:{_prefix()}payloads"


#: One round trip for the whole observable set. The queue is a LIST and the
#: consumer heartbeats a HASH, so age has to come from the data rather than from
#: a key's TTL: the oldest job is the right-most entry (claim RPOPs, enqueue
#: LPUSHes) and its enqueued_at is in the payload hash.
_OBSERVABLES_LUA = """
local q = redis.call('LLEN', KEYS[1])
local delayed = redis.call('ZCARD', KEYS[2])
local claims = redis.call('ZCARD', KEYS[3])
local now = tonumber(ARGV[1])
local stale_after = tonumber(ARGV[2])
local consumers = 0
local stale = 0
local oldest_consumer = -1
local entries = redis.call('HGETALL', KEYS[4])
for i = 1, #entries, 2 do
  consumers = consumers + 1
  local beat = string.match(entries[i], '^(%d+%.?%d*)')
  if beat then
    local age = now - tonumber(beat)
    if age > oldest_consumer or oldest_consumer < 0 then oldest_consumer = age end
    if age > stale_after then stale = stale + 1 end
  else
    stale = stale + 1
  end
end
local oldest = -1
local last = redis.call('LINDEX', KEYS[1], -1)
if last then
  local raw = redis.call('HGET', KEYS[5], last)
  if raw then
    local ok, decoded = pcall(cjson.decode, raw)
    if ok and decoded['enqueued_at'] then
      oldest = now - tonumber(decoded['enqueued_at'])
    end
  end
end
return {q, delayed, claims, oldest, consumers, stale, oldest_consumer}
"""


# --------------------------------------------------------------------------
# supervision
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SupervisorState:
    """Restart bookkeeping, so a crash loop is countable rather than silent."""

    consumer_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    consecutive_failures: int = 0
    restarts: int = 0
    started_at: float = field(default_factory=_now)


def _default_consumer() -> object:
    """The real extraction loop, imported lazily.

    Lazy so a worker with GRAPH_MODE=off never imports graph extraction code at
    all, matching the flag-off discipline the rest of the graph layer keeps.
    """
    from jobs.graph_extraction import run_graph_worker

    return run_graph_worker


async def run_supervised_graph_consumer(
    stop_event: asyncio.Event,
    *,
    consumer: object | None = None,
    heartbeat_interval: float = 5.0,
    stale_after: float = DEFAULT_HEARTBEAT_STALE_SECONDS,
    backoff: float = DEFAULT_RESTART_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_MAX_BACKOFF_SECONDS,
    max_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
) -> SupervisorState:
    """Run the graph consumer under a restart policy until ``stop_event``.

    A crashing consumer is restarted with growing backoff up to
    ``max_failures`` consecutive failures, after which supervision stops and the
    worker keeps serving ingestion rather than hot-looping on a poison job.
    Every restart is logged with its count, so "the graph consumer is
    crash-looping" is a visible line rather than an inference.

    ``consumer`` defaults to the real :func:`run_graph_worker`; tests inject a
    fake. Injected consumers receive ``stop_event`` only if they accept it.
    """
    if consumer is None:
        consumer = _default_consumer()

    state = SupervisorState()
    logger.info(
        "graph_consumer_supervising",
        extra={
            "event": "graph_consumer_supervising",
            "consumer_id": state.consumer_id,
            "mode": graph_mode(),
        },
    )
    await publish_heartbeat(consumer_id=state.consumer_id, state="starting")

    while not stop_event.is_set():
        heartbeat = asyncio.create_task(_heartbeat_loop(stop_event, state, heartbeat_interval))
        try:
            await _invoke_consumer(consumer, stop_event)
            # A clean return means the consumer finished; only restart if we
            # were not asked to stop.
            if stop_event.is_set():
                break
            state.consecutive_failures += 1
            state.restarts += 1
            logger.warning(
                "graph_consumer_restarting",
                extra={
                    "event": "graph_consumer_restarting",
                    "consumer_id": state.consumer_id,
                    "restarts": state.restarts,
                    "consecutive_failures": state.consecutive_failures,
                    "reason": "consumer returned without being stopped",
                },
            )
        except asyncio.CancelledError:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            raise
        except Exception as exc:
            state.consecutive_failures += 1
            state.restarts += 1
            logger.error(
                "graph_consumer_crashed",
                extra={
                    "event": "graph_consumer_crashed",
                    "consumer_id": state.consumer_id,
                    "restarts": state.restarts,
                    "consecutive_failures": state.consecutive_failures,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

        if state.consecutive_failures >= max_failures:
            logger.error(
                "graph_consumer_supervision_abandoned",
                extra={
                    "event": "graph_consumer_supervision_abandoned",
                    "consumer_id": state.consumer_id,
                    "consecutive_failures": state.consecutive_failures,
                    "note": "graph extraction stops; ingestion continues",
                },
            )
            await publish_heartbeat(consumer_id=state.consumer_id, state="abandoned")
            break

        delay = min(backoff * (2 ** (state.consecutive_failures - 1)), max_backoff)
        await _sleep_or_stop(stop_event, delay)

    await publish_heartbeat(consumer_id=state.consumer_id, state="stopped")
    logger.info(
        "graph_consumer_stopped",
        extra={
            "event": "graph_consumer_stopped",
            "consumer_id": state.consumer_id,
            "restarts": state.restarts,
        },
    )
    return state


async def _invoke_consumer(consumer: object, stop_event: asyncio.Event) -> None:
    """Call the consumer with ``stop_event`` only if it accepts one."""
    try:
        result = consumer(stop_event)  # type: ignore[operator]
    except TypeError:
        result = consumer()  # type: ignore[operator]
    if asyncio.iscoroutine(result):
        await result


async def _heartbeat_loop(
    stop_event: asyncio.Event,
    state: SupervisorState,
    interval: float,
) -> None:
    """Beat while the consumer runs, and log the observable snapshot.

    The snapshot is logged rather than only stored: an operator watching logs
    should be able to see queue depth and consumer count without a separate
    metrics scrape, and vice versa.
    """
    while not stop_event.is_set():
        observables = await collect_queue_observables()
        logger.info(
            "graph_queue_observables",
            extra={"event": "graph_queue_observables", **observables.as_extra()},
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval)


async def _sleep_or_stop(stop_event: asyncio.Event, delay: float) -> None:
    """Sleep, but wake immediately on shutdown instead of holding the process."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=delay)