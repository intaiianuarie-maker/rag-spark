"""Redis-backed ingestion job queue (public API, M2-001c).

Shared by the API (enqueue on upload) and the worker (consume + delayed retry).
The vendor Redis client lives in ``ingestion.adapters.redis_queue`` (CI vendor
fence); this module is the typed surface both services import. Keys are
namespaced by environment so tests never share a queue with a live worker.
"""

from __future__ import annotations

from ingestion.adapters.redis_queue import (
    IngestQueueUnavailable,
    close_client,
    delayed_key,
    dequeue,
    enqueue,
    get_client,
    promote_due,
    queue_depth,
    queue_key,
    schedule_retry,
)

__all__ = [
    "IngestQueueUnavailable",
    "close_client",
    "delayed_key",
    "dequeue",
    "enqueue",
    "get_client",
    "promote_due",
    "queue_depth",
    "queue_key",
    "schedule_retry",
]
