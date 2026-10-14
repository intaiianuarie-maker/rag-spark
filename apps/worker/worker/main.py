"""Worker entrypoint (M0 stub).

Boots, connects to Redis, logs that no jobs are registered yet, and idles until
SIGINT/SIGTERM. Real ingestion/evaluation jobs arrive in later milestones.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from core.config import get_settings
from core.logging import configure_logging

# Imported eagerly but side-effect free: consumer_enabled()/graph_mode() are
# plain env reads, and flag-off never constructs a consumer.
from graph.consumer import (
    consumer_enabled,
    graph_mode,
    run_supervised_graph_consumer,
)
from jobs.ingest_worker import run_worker

from worker.adapters.redis_broker import RedisBroker

logger = logging.getLogger("knowledge_os.worker")


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

    logger.info("ingest jobs registered (M2-001c)")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX platforms
            pass

    worker_task = asyncio.create_task(run_worker(stop))

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
        await broker.close()
        logger.info("worker_stopped", extra={"event": "worker_stopped"})


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        pass


if __name__ == "__main__":
    main()
