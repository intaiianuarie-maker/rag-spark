"""Worker entrypoint (M0 stub).

Boots, connects to Redis, logs that no jobs are registered yet, and idles until
SIGINT/SIGTERM. Real ingestion/evaluation jobs arrive in later milestones.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import NamedTuple

from core.config import get_settings
from core.logging import configure_logging

# Imported eagerly but side-effect free: consumer_enabled()/graph_mode() are
# plain env reads, and flag-off never constructs a consumer.
from graph.consumer import (
    consumer_enabled,
    graph_mode,
    run_supervised_graph_consumer,
)
from jobs.agent_worker import run_supervised_agent_consumer
from jobs.ingest_worker import run_worker

from worker.adapters.redis_broker import RedisBroker

logger = logging.getLogger("knowledge_os.worker")

#: Environments where a process-local (in-memory) agent store is tolerated.
#: Production and staging must use the shared durable store (T-IMP-001).
LOCAL_ENVIRONMENTS = frozenset({"local", "test", "ci", ""})

#: Default shared root of the durable agent store. In the compose deployment
#: this is the named volume mounted into BOTH api and worker
#: (``agentdata:/app/.agent``); a path outside it is not shared storage.
DEFAULT_AGENT_STORE_SHARED_ROOT = "/app/.agent"


class AgentStoreReadiness(NamedTuple):
    """Resolved readiness of the durable agent store (mirrors the API side)."""

    admit: bool
    shared: bool
    reason: str
    path: str
    environment: str


def shared_root() -> str:
    """Return the shared root the durable agent store must live in."""
    return os.environ.get("AGENT_STORE_SHARED_ROOT", "").strip() or (
        DEFAULT_AGENT_STORE_SHARED_ROOT
    )


def agent_store_readiness() -> AgentStoreReadiness:
    """Resolve the durable agent-store readiness (fail-closed).

    Mirrors ``app.agents.lifecycle_routes.agent_store_readiness`` exactly: the
    environment comes from the RESOLVED application settings (process env or
    ``.env``), and a configured path is only shared when it lives inside the
    shared root. The worker and the API must agree, otherwise the API accepts
    runs the worker can never see (POLICE F4a/F4b).
    """
    settings = get_settings()
    environment = str(settings.app_env).strip().lower()
    raw_path = os.environ.get("AGENT_STORE_DB", "").strip()
    root = shared_root()
    local = environment in LOCAL_ENVIRONMENTS

    if not raw_path:
        return AgentStoreReadiness(
            admit=local,
            shared=False,
            reason="agent_store_not_configured",
            path="",
            environment=environment,
        )

    shared = _is_inside(raw_path, root)
    if shared:
        return AgentStoreReadiness(
            admit=True,
            shared=True,
            reason="agent_store_shared",
            path=raw_path,
            environment=environment,
        )
    return AgentStoreReadiness(
        admit=local,
        shared=False,
        reason=(
            "agent_store_unshared_dev_only" if local else "agent_store_unshared"
        ),
        path=raw_path,
        environment=environment,
    )


def _is_inside(path: str, root: str) -> bool:
    """Return True when ``path`` resolves inside ``root``."""
    try:
        resolved = Path(path).resolve()
        resolved_root = Path(root).resolve()
    except OSError:  # pragma: no cover - unresolvable path
        return False
    return resolved == resolved_root or resolved_root in resolved.parents


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    logger.info(
        "worker_starting",
        extra={"event": "worker_starting", "env": settings.app_env, "version": settings.version},
    )

    broker = RedisBroker(settings.redis_url)
    connected = await broker.connect_with_retry(timeout_seconds=30.0)
    if connected:
        logger.info("worker_redis_connected", extra={"event": "worker_redis_connected"})
    else:
        logger.warning("worker_redis_unavailable", extra={"event": "worker_redis_unavailable"})

    from stores.agent_stores import set_agent_store_db

    # T-IMP-001 (POLICE F4a/F4b): the worker resolves the durable agent store
    # through the same settings/startup boundary as the API. A path outside
    # the shared root is NOT shared storage (two containers, two filesystems),
    # so the worker refuses to consume from it instead of silently seeing an
    # empty queue.
    readiness = agent_store_readiness()
    agent_db = readiness.path
    if readiness.admit and agent_db:
        set_agent_store_db(agent_db)
        logger.info(
            "agent_store_installed",
            extra={
                "event": "agent_store_installed",
                "path": agent_db,
                "shared": readiness.shared,
                "environment": readiness.environment,
            },
        )
    elif readiness.shared:
        logger.warning(
            "agent_store_in_memory",
            extra={
                "event": "agent_store_in_memory",
                "reason": "AGENT_STORE_DB unset; queued runs are process-local (dev/test only)",
                "environment": readiness.environment,
            },
        )
    else:
        logger.error(
            "agent_store_unavailable",
            extra={
                "event": "agent_store_unavailable",
                "reason": readiness.reason,
                "path": agent_db,
                "environment": readiness.environment,
                "shared_root": shared_root(),
            },
        )

    logger.info("ingest jobs registered (M2-001c)")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX platforms
            pass

    worker_task = asyncio.create_task(run_worker(stop))

    agent_task: asyncio.Task | None = None
    if readiness.admit:
        agent_task = asyncio.create_task(
            run_supervised_agent_consumer(stop), name="agent-lifecycle-consumer"
        )
    else:
        # Fail closed: consuming from a store the API does not share would
        # silently strand every accepted run (POLICE F4b).
        logger.error(
            "agent_consumer_disabled",
            extra={
                "event": "agent_consumer_disabled",
                "reason": readiness.reason,
                "environment": readiness.environment,
            },
        )

    # T-R11: the graph extraction consumer, supervised. GRAPH_MODE=off (the
    # default) starts nothing and touches no graph code, so the flag-off path
    # stays byte-identical to before. When on, the consumer runs beside
    # ingestion, and a crash restarts it with backoff instead of taking the
    # worker down — or, worse, vanishing without a trace.
    graph_task: asyncio.Task | None = None
    if consumer_enabled():
        graph_task = asyncio.create_task(
            run_supervised_graph_consumer(stop),
            name="graph-consumer",
        )
        logger.info(
            "graph_consumer_enabled",
            extra={"event": "graph_consumer_enabled", "graph_mode": graph_mode()},
        )
    else:
        logger.info(
            "graph_consumer_disabled",
            extra={"event": "graph_consumer_disabled", "graph_mode": graph_mode()},
        )

    try:
        await stop.wait()
    finally:
        stop.set()
        await worker_task
        if graph_task is not None:
            # The supervisor also honours stop_event, so this returns promptly.
            await graph_task
        if agent_task is not None:
            await agent_task
        await broker.close()
        logger.info("worker_stopped", extra={"event": "worker_stopped"})


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        pass


if __name__ == "__main__":
    main()
