"""Aggregate dependency readiness checks for ``GET /ready`` (T-V1-027).

Probes every dependency the *enabled modes* actually require — concurrently,
each with a bounded timeout and an outer freshness bound. Failures are
reported, never raised, so readiness stays a 200 with per-service status (M0
requirement: "non-fatal with status"). The API container is still considered
started even if a dependency is down, letting the UI show an explicit degraded
state.

**What is probed — the exact set.** ``core.errors.DEPENDENCY_MATRIX``
(T-V1-025) is the single source of truth for required-vs-optional per mode.
Enabled modes are ``search``/``ask``/``ingest`` (always on) plus ``graph`` when
``GRAPH_MODE`` is ``rule``/``llm``; the map below is the union of their REQUIRED
dependencies:

===================  =====================================================
probe                capability
===================  =====================================================
``postgres``         metadata + FTS + job rows (every mode)
``redis``            ingest queue/broker (required by ingest)
``qdrant``           dense vectors (search/ask/ingest)
``minio``            document originals (ingest)
``infinity``         embeddings **and** the reranker — one service, one
                     probe (PD-002). The rerank leg is *optional* and
                     degrades at leg level (``RERANK_UNAVAILABLE``), so it
                     is disclosed here instead of probed twice; that is
                     the optional fallback this map does not hide.
``llm_gateway``      the model (Ask generation)
``worker``           the ingest queue consumer, observed by the freshness
                     of the heartbeat key it renews each loop iteration
                     (T-V1-056 — since T-V1-020's claim/ack it holds no
                     standing ``BRPOP`` client, so a ``CLIENT LIST`` check
                     would read a healthy worker as down)
``graph_store``      only when the graph mode is enabled
===================  =====================================================

Because only REQUIRED dependencies are in the map, ``/ready``'s existing
``all(dep.reachable)`` verdict *is* "required-down ⇒ not-ready". Optional
dependencies are disclosed rather than silently ignored, and a required
dependency with no probe reads **red** (``no probe registered``) — never green.

**Timeouts + freshness.** Store probes use ``READINESS_TIMEOUT_SECONDS``,
capability probes ``READINESS_CAPABILITY_TIMEOUT_SECONDS``: per-probe transport
budgets. ``READINESS_FRESHNESS_SECONDS`` is the outer bound — no reported
result may be older than it. A probe still running at that point is cancelled
and reported unreachable with an explicit ``freshness`` error, so a hung
dependency can neither stall ``/ready`` nor produce a stale "ready". Nothing is
cached between calls: every ``/ready`` re-probes live, which is what keeps
"ready-during-outage: never" true (a cached success could outlive its service).

**Model gateway (review1 F3 → unblocked by T-V1-027).** F3 blocked probing
because ``LITELLM_URL`` is an external SaaS endpoint (or a coordinator-owned
``:4000`` topology) with no cheap contractually-guaranteed health route, and a
billable completion endpoint must not be hit on every readiness poll. This
probe keeps F3's substance: a bare ``GET`` on a health path, no API key, no
completion, no tokens, bounded by the capability timeout, never cached. The
reachability rule is the one T-V1-005 established for infinity — any sub-500
answer proves the process is up (a gateway without that route answers 404),
while a 5xx/timeout/refusal reads red, so an unreachable or unconfigured
gateway can never look ready.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, Literal

import httpx
import redis.asyncio as redis_asyncio
from core.config import get_settings
from core.errors import Dependency, ServiceMode, required_dependencies
from core.health import DependencyStatus

from app.adapters import infinity_probe, minio_probe, postgres, qdrant_probe, redis_probe

#: ``GRAPH_MODE`` values that enable the graph leg — mirrors
#: ``packages/graph/factory.py::GRAPH_MODES_ON`` so readiness stays a
#: dependency check instead of importing the graph stack.
GRAPH_MODES_ON = frozenset({"rule", "llm"})

#: Exact ``/ready`` dependency names with the graph mode off (the test/CI
#: default). ``tests/fixtures/scripts/m004_ready_degraded.py``,
#: ``tests/integration/test_boot.py`` and
#: ``tests/integration/test_live_endpoints.py`` pin this same set.
BASE_DEPENDENCIES: tuple[str, ...] = (
    "postgres",
    "redis",
    "qdrant",
    "minio",
    "infinity",
    "llm_gateway",
    "worker",
)

GraphProbeState = Literal["enabled", "off", "invalid"]


def graph_probe_state(graph_mode: str) -> GraphProbeState:
    """Whether ``graph_store`` belongs in the ``/ready`` map for ``graph_mode``.

    ``invalid`` (a typo'd ``GRAPH_MODE``) still reports the dependency — red,
    with the config error — instead of dropping it: the graph leg would raise
    at request time, so readiness must not read green.
    """
    normalized = graph_mode.strip().lower()
    if normalized in {"", "off"}:
        return "off"
    return "enabled" if normalized in GRAPH_MODES_ON else "invalid"


def probe_names(graph_mode: str) -> tuple[str, ...]:
    """Exact dependency names ``/ready`` reports for ``graph_mode``.

    The pinned contract: the three degraded-contract places assert this set
    (as a literal) and ``/ready`` must report exactly it.
    """
    names = list(BASE_DEPENDENCIES)
    if graph_probe_state(graph_mode) != "off":
        names.append(Dependency.GRAPH_STORE.value)
    return tuple(names)


def enabled_modes(graph_mode: str) -> frozenset[ServiceMode]:
    """Modes this deployment serves; ``graph`` only when ``GRAPH_MODE`` is on."""
    modes: set[ServiceMode] = {ServiceMode.SEARCH, ServiceMode.ASK, ServiceMode.INGEST}
    if graph_probe_state(graph_mode) == "enabled":
        modes.add(ServiceMode.GRAPH)
    return frozenset(modes)


def required_dependencies_for_modes(graph_mode: str) -> set[str]:
    """Every dependency an enabled mode fails closed without (T-V1-025)."""
    return {
        dependency.value
        for mode in enabled_modes(graph_mode)
        for dependency in required_dependencies(mode)
    }


async def _unprobed(name: str) -> DependencyStatus:
    """Fail closed: a required dependency without a probe reads red, never green."""
    return DependencyStatus(
        name=name,
        reachable=False,
        error="no probe registered for a required dependency (fail closed)",
    )


async def _invalid_graph_mode(graph_mode: str) -> DependencyStatus:
    """A ``GRAPH_MODE`` typo is a config error: report it, do not drop it."""
    return DependencyStatus(
        name=Dependency.GRAPH_STORE.value,
        reachable=False,
        error=f"GRAPH_MODE={graph_mode!r} is not one of off/rule/llm (config error, fail closed)",
    )


class _ModelGatewayProbe:
    """Health-only probe of the completion gateway ("model").

    A bare ``GET {LITELLM_URL}{READINESS_MODEL_HEALTH_PATH}`` — no API key, no
    completion, no tokens — under the same sub-500 rule as the infinity probe
    (T-V1-005): an answer below 500 proves the gateway is up, a 5xx/timeout/
    refusal reads red. An empty/unconfigured URL is unreachable (fail closed).
    """

    async def probe(
        self, base_url: str, health_path: str, timeout_seconds: float
    ) -> DependencyStatus:
        name = Dependency.LLM_GATEWAY.value
        start = time.perf_counter()
        base = base_url.strip()
        if not base:
            return DependencyStatus(
                name=name,
                reachable=False,
                error="LITELLM_URL is empty: no model gateway configured (fail closed)",
            )
        if not base.startswith(("http://", "https://")):
            base = f"http://{base}"
        path = health_path.strip()
        if path and not path.startswith("/"):
            path = f"/{path}"
        url = base.rstrip("/") + path
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.get(url)
            latency = (time.perf_counter() - start) * 1000
            return DependencyStatus(
                name=name,
                # T-V1-005 rule: any sub-500 answer proves the process is up.
                reachable=response.status_code < 500,
                latency_ms=round(latency, 2),
                detail=f"GET {path or '/'} -> {response.status_code}",
            )
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            return DependencyStatus(
                name=name,
                reachable=False,
                latency_ms=round(latency, 2),
                error=f"{type(exc).__name__}: {exc}",
            )


def _worker_heartbeat_key() -> str:
    """The broker key the worker renews as its liveness signal (T-V1-056).

    Imported rather than re-derived so the writer (``jobs.ingest_worker``) and
    this reader cannot drift apart on the key or on the ``INGEST_QUEUE_PREFIX``
    that scopes it.
    """
    from ingestion.adapters.redis_queue import _prefix

    return f"ingest:{_prefix()}worker_heartbeat"


def _worker_heartbeat_bound() -> float:
    """How old a heartbeat may be and still read as live.

    Twice the worker's TTL: one missed refresh (a long job tick, a GC pause)
    cannot flap the probe red, while a stopped worker still fails the check
    within roughly two TTLs.
    """
    raw = os.environ.get("INGEST_WORKER_HEARTBEAT_TTL_SECONDS")
    ttl = 15.0
    if raw:
        try:
            value = float(raw)
            if value > 0:
                ttl = value
        except ValueError:
            pass
    return ttl * 2


class _WorkerLivenessProbe:
    """Is a live ingest worker renewing its heartbeat (T-V1-056)?

    The worker has no HTTP surface, so its liveness is observed on the broker
    it consumes from. Since T-V1-020 the worker is a durable claim/ack loop of
    short non-blocking polls, so it holds **no** standing blocked ``BRPOP``
    client — the previous probe (``CLIENT LIST`` for a ``brpop`` consumer)
    therefore reported a perfectly healthy worker as down: a false red that
    would also mask a genuinely dead one behind a permanently red check.

    The worker instead renews a short-TTL key once per loop iteration
    (``jobs.ingest_worker.write_heartbeat``) and this probe reads its
    freshness, failing closed on every non-fresh path:

    * key **absent** — nothing renewed within the TTL ⇒ not reachable;
    * key **stale** — the embedded timestamp is older than the bound ⇒
      not reachable (covers a key whose TTL was written longer);
    * key **unreadable / broker error** — not reachable.

    The bound is twice the heartbeat TTL (``_worker_heartbeat_bound``), so one
    missed refresh cannot flap the probe red, while a stopped worker still
    fails within roughly two TTLs.

    Disclosed limitation, unchanged in kind from before: the key proves *a*
    worker is running against that broker and prefix, not a specific PID.
    """

    async def probe(self, redis_url: str, timeout_seconds: float) -> DependencyStatus:
        name = Dependency.WORKER.value
        start = time.perf_counter()
        client = redis_asyncio.from_url(
            redis_url,
            socket_connect_timeout=timeout_seconds,
            socket_timeout=timeout_seconds,
            decode_responses=True,
        )
        try:
            raw = await asyncio.wait_for(
                client.get(_worker_heartbeat_key()), timeout=timeout_seconds
            )
            latency = (time.perf_counter() - start) * 1000
            if raw is None:
                return DependencyStatus(
                    name=name,
                    reachable=False,
                    latency_ms=round(latency, 2),
                    error="no worker heartbeat on the broker (worker is not renewing)",
                )
            try:
                age = time.time() - float(raw)
            except (TypeError, ValueError):
                return DependencyStatus(
                    name=name,
                    reachable=False,
                    latency_ms=round(latency, 2),
                    error="worker heartbeat is unreadable (not an epoch timestamp)",
                )
            bound = _worker_heartbeat_bound()
            if age > bound:
                return DependencyStatus(
                    name=name,
                    reachable=False,
                    latency_ms=round(latency, 2),
                    error=(
                        f"worker heartbeat is stale ({age:.1f}s old, "
                        f"bound {bound:.0f}s)"
                    ),
                )
            return DependencyStatus(
                name=name,
                reachable=True,
                latency_ms=round(latency, 2),
                detail=f"worker heartbeat renewed {max(age, 0.0):.1f}s ago",
            )
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            return DependencyStatus(
                name=name,
                reachable=False,
                latency_ms=round(latency, 2),
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover - best-effort close
                pass


def _graph_store_state(graph_db_path: str) -> tuple[bool, str]:
    """Sync filesystem check for the embedded SQLite graph store.

    ``packages/graph/sqlite_store.py`` creates the parent directory on first
    use, so an absent-but-creatable database file is healthy; a path whose
    directory cannot be created or written reads red.
    """
    raw = graph_db_path.strip()
    if not raw:
        return False, "GRAPH_DB_PATH is empty (fail closed)"
    path = Path(raw)
    try:
        if path.is_dir():
            return False, f"{raw} is a directory, not a sqlite file"
        if path.is_file():
            with path.open("rb") as handle:
                handle.read(16)
            return True, f"sqlite file present ({raw})"
        ancestor = path.parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if not ancestor.exists():
            return False, f"no existing parent directory for {raw}"
        if not ancestor.is_dir() or not os.access(ancestor, os.W_OK):
            return False, f"graph store directory not writable: {ancestor}"
        return True, f"store ready, db created on first write ({raw})"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


class _GraphStoreProbe:
    """Graph store readiness — probed only when the graph mode is enabled."""

    async def probe(self, graph_db_path: str, timeout_seconds: float) -> DependencyStatus:
        name = Dependency.GRAPH_STORE.value
        start = time.perf_counter()
        try:
            reachable, detail = await asyncio.wait_for(
                asyncio.to_thread(_graph_store_state, graph_db_path),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            return DependencyStatus(
                name=name,
                reachable=False,
                latency_ms=round(latency, 2),
                error=f"{type(exc).__name__}: {exc}",
            )
        latency = (time.perf_counter() - start) * 1000
        return DependencyStatus(
            name=name,
            reachable=reachable,
            latency_ms=round(latency, 2),
            detail=detail if reachable else None,
            error=None if reachable else detail,
        )


model_gateway = _ModelGatewayProbe()
worker_liveness = _WorkerLivenessProbe()
graph_store = _GraphStoreProbe()


async def _gather_fresh(
    probes: dict[str, Coroutine[Any, Any, DependencyStatus]],
    freshness_seconds: float,
) -> dict[str, DependencyStatus]:
    """Run every probe concurrently under the freshness bound.

    Anything still running when the bound expires is cancelled and reported
    unreachable: a result older than ``freshness_seconds`` is stale, and stale
    is never ready.
    """
    tasks: dict[str, asyncio.Task[DependencyStatus]] = {
        name: asyncio.ensure_future(coro) for name, coro in probes.items()
    }
    await asyncio.wait(tasks.values(), timeout=freshness_seconds)

    statuses: dict[str, DependencyStatus] = {}
    stale: list[asyncio.Task[DependencyStatus]] = []
    for name, task in tasks.items():
        if not task.done():
            task.cancel()
            stale.append(task)
            statuses[name] = DependencyStatus(
                name=name,
                reachable=False,
                error=(
                    f"freshness: no result within {freshness_seconds}s "
                    "(a stale result is never reported as ready)"
                ),
            )
            continue
        if task.cancelled():
            statuses[name] = DependencyStatus(name=name, reachable=False, error="cancelled")
            continue
        failure = task.exception()
        if failure is not None:
            statuses[name] = DependencyStatus(
                name=name,
                reachable=False,
                error=f"{type(failure).__name__}: {failure}",
            )
            continue
        statuses[name] = task.result()
    if stale:
        await asyncio.gather(*stale, return_exceptions=True)
    return statuses


async def check_dependencies() -> dict[str, DependencyStatus]:
    """Probe every dependency the enabled modes require (exact set, see module)."""
    settings = get_settings()
    timeout = settings.readiness_timeout_seconds
    capability_timeout = settings.readiness_capability_timeout_seconds
    freshness = settings.readiness_freshness_seconds
    state = graph_probe_state(settings.graph_mode)

    probes: dict[str, Coroutine[Any, Any, DependencyStatus]] = {
        "postgres": postgres.probe(timeout),
        "redis": redis_probe.probe(settings.redis_url, timeout),
        "qdrant": qdrant_probe.probe(settings.qdrant_url, timeout),
        "minio": minio_probe.probe(settings.minio_endpoint, timeout, settings.minio_secure),
        "infinity": infinity_probe.probe(settings.infinity_url, timeout),
        "llm_gateway": model_gateway.probe(
            settings.litellm_url, settings.readiness_model_health_path, capability_timeout
        ),
        "worker": worker_liveness.probe(settings.redis_url, capability_timeout),
        # The graph entry exists only when the mode is on (a coroutine built
        # unconditionally would never be awaited); an invalid GRAPH_MODE still
        # reports it — red — rather than dropping the dependency.
        **(
            {"graph_store": graph_store.probe(settings.graph_db_path, capability_timeout)}
            if state == "enabled"
            else {}
        ),
    }
    if state == "invalid":
        probes["graph_store"] = _invalid_graph_mode(settings.graph_mode)

    # Fail closed: a dependency the taxonomy calls REQUIRED for an enabled mode
    # must never disappear from the report just because no probe is wired yet.
    for name in sorted(required_dependencies_for_modes(settings.graph_mode) - probes.keys()):
        probes[name] = _unprobed(name)

    return await _gather_fresh(probes, freshness)
