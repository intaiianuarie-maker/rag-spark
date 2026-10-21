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

#: The source bind mounts the compose deployment puts inside the container
#: image tree. A shared root that covers one of them is NOT shared storage:
#: the agent volume is only present where nothing else is bound, so the store
#: file would live in a per-container filesystem and api and worker would
#: open different files (POLICE F4b, P2 shadowing finding).
CONTAINER_BIND_MOUNTS = ("/app/apps/api", "/app/apps/worker", "/app/packages")

#: The mount table the readiness consults. Overridable so the definitions can
#: be probed with a synthetic mountinfo file (no container required).
MOUNTINFO_PATH_ENV = "AGENT_STORE_MOUNTINFO"
DEFAULT_MOUNTINFO_PATH = "/proc/self/mountinfo"


class AgentStoreReadiness(NamedTuple):
    """Resolved readiness of the durable agent store (mirrors the API side)."""

    admit: bool
    shared: bool
    reason: str
    path: str
    environment: str


def _same_path(left: str, right: str) -> bool:
    """Return True when both paths resolve to the same location."""
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:  # pragma: no cover - unresolvable path
        return False


def shared_root() -> str:
    """Return the shared root the durable agent store must live in."""
    return os.environ.get("AGENT_STORE_SHARED_ROOT", "").strip() or (
        DEFAULT_AGENT_STORE_SHARED_ROOT
    )


def shared_mount_target() -> str:
    """Return the path the shared agent volume is actually mounted at."""
    return os.environ.get("AGENT_STORE_SHARED_MOUNT", "").strip()


def root_is_mounted(root: str) -> bool:
    """Return True when ``root`` is the mounted shared volume.

    Mirrors ``app.agents.lifecycle_routes.root_is_mounted``: a resolved root
    that is not where the shared volume is mounted is NOT shared storage
    (POLICE F4b).
    """
    mount_target = shared_mount_target()
    if not mount_target:
        return True
    try:
        return Path(mount_target).resolve() == Path(root).resolve()
    except OSError:  # pragma: no cover - unresolvable path
        return False


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

    if root_is_shadowed(root):
        return AgentStoreReadiness(
            admit=local,
            shared=False,
            reason=(
                "agent_store_root_shadowed_dev_only"
                if local
                else "agent_store_root_shadowed"
            ),
            path=raw_path,
            environment=environment,
        )

    declared_mount = shared_mount_target()
    if declared_mount:
        if not _same_path(declared_mount, root):
            # The declared mount is not the resolved root: the store would
            # live somewhere the other process cannot see.
            return AgentStoreReadiness(
                admit=local,
                shared=False,
                reason="agent_store_root_not_mounted",
                path=raw_path,
                environment=environment,
            )
        # A DECLARED shared mount must be proven by a readable mount table
        # that contains an entry AT that mount point. Without the evidence
        # the store may be a per-container file (POLICE F4b, P2).
        if not mount_evidence_ok(declared_mount):
            return AgentStoreReadiness(
                admit=local,
                shared=False,
                reason=(
                    "agent_store_mount_unverified_dev_only"
                    if local
                    else "agent_store_mount_unverified"
                ),
                path=raw_path,
                environment=environment,
            )

    shared = _is_inside(raw_path, root) and root_is_mounted(root)
    if shared:
        return AgentStoreReadiness(
            admit=True,
            shared=True,
            reason="agent_store_shared",
            path=raw_path,
            environment=environment,
        )
    reason = (
        "agent_store_unshared_dev_only" if local else "agent_store_unshared"
    )
    if not root_is_mounted(root):
        reason = (
            "agent_store_root_not_mounted_dev_only"
            if local
            else "agent_store_root_not_mounted"
        )
    return AgentStoreReadiness(
        admit=local,
        shared=False,
        reason=reason,
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

def shadow_mounts() -> list[str]:
    """Return the mount targets that can shadow the shared agent volume.

    The compose deployment bind-mounts the api/worker sources and the shared
    graph/workflow volumes into the container. When the resolved shared root
    covers one of those subtrees, the agent volume is only present where
    nothing else is bound: the store file would live in a per-container
    filesystem and the two processes would open different files
    (POLICE F4b, P2 shadowing finding).
    """
    raw = os.environ.get("AGENT_STORE_SHADOW_MOUNTS", "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


def _unescape_mountinfo_path(value: str) -> str:
    """Decode the kernel's octal escapes in ONE pass.

    The kernel escapes the characters that would break the whitespace
    separated mountinfo format: space (\\040), tab (\\011), newline
    (\\012) and backslash (\\134) are written as octal sequences. Decoding
    only the space escape misses a nested mount under a root that contains a
    backslash or a tab, so the store would be admitted while it actually
    lives on a shadowed filesystem (POLICE F4b, P2).

    The pass is left to right and the decoded bytes are never re-scanned, so
    a literal ``\\134040`` decodes to ``\\040`` (a backslash followed by
    "040") and not to a space (Moby ``sys/mountinfo`` semantics).
    """
    if "\\" not in value:
        return value
    out: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        char = value[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 3 < length and all(
            c in "01234567" for c in value[index + 1 : index + 4]
        ):
            out.append(chr(int(value[index + 1 : index + 4], 8)))
            index += 4
            continue
        if index + 1 < length and value[index + 1] in " \t\n":
            out.append(value[index + 1])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _mount_points() -> list[str]:
    """Return the mount points of this process (best effort).

    The mount table can be redirected with ``AGENT_STORE_MOUNTINFO`` so the
    definitions can be probed with a synthetic mountinfo file (no container
    required). An unreadable table yields NO points: a declared shared mount
    then has no evidence and is refused.
    """
    path = (
        os.environ.get(MOUNTINFO_PATH_ENV, "").strip() or DEFAULT_MOUNTINFO_PATH
    )
    try:
        with open(path, encoding="utf-8") as handle:
            points = []
            for line in handle:
                fields = line.split(" ")
                # 5th field is the mount point (octal escaped by the kernel).
                if len(fields) > 4:
                    points.append(_unescape_mountinfo_path(fields[4]))
            return points
    except OSError:  # pragma: no cover - not Linux / unreadable
        return []


def mount_evidence_ok(target: str) -> bool:
    """Return True when the mount table proves the intended mount exists.

    A DECLARED shared mount (``AGENT_STORE_SHARED_MOUNT``) must be backed by
    a readable mount table that contains an entry AT that mount point. Absent
    or unreadable evidence is a refusal, not a pass: without it the store
    could be a per-container file (POLICE F4b, P2).
    """
    points = _mount_points()
    if not points:
        return False
    try:
        resolved_target = Path(target).resolve()
    except OSError:  # pragma: no cover - unresolvable path
        return False
    for point in points:
        try:
            if Path(point).resolve() == resolved_target:
                return True
        except OSError:  # pragma: no cover - unresolvable path
            continue
    return False


def root_is_shadowed(root: str) -> bool:
    """Return True when another mount covers part of ``root``.

    Mirrors ``app.agents.lifecycle_routes.root_is_shadowed``: the INTENDED
    shared mount sits at the root's own mount point and does not shadow it;
    a competing mount inside the root (or the root itself being a known
    bind mount) does (POLICE F4b).
    """
    try:
        resolved_root = Path(root).resolve()
    except OSError:  # pragma: no cover - unresolvable path
        return True

    for candidate in list(CONTAINER_BIND_MOUNTS) + shadow_mounts():
        try:
            resolved = Path(candidate).resolve()
        except OSError:  # pragma: no cover - unresolvable path
            continue
        if resolved == resolved_root or resolved_root in resolved.parents:
            # A known competing mount covers the root or lives inside it.
            return True

    for candidate in _mount_points():
        try:
            resolved = Path(candidate).resolve()
        except OSError:  # pragma: no cover - unresolvable path
            continue
        if resolved_root in resolved.parents:
            # A mount lives INSIDE the root: the shared volume is shadowed
            # there, so the store would not be on the shared filesystem.
            return True
        # A mount AT the root is the intended shared volume; a mount ABOVE
        # the root is the filesystem the root lives on. Neither shadows it.
    return False


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
