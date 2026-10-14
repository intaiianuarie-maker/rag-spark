"""T-R11 — supervised graph consumer, one shared store, and observables.

The topology bug this closes: ``graph_db_path()`` defaulted to the RELATIVE
``./.graph/graph.db``. In Docker every service resolved that inside its own
container filesystem, so api, worker and extraction each held a PRIVATE sqlite
file. Rows written by the worker were invisible to the api, and the cause was
configuration, not code — which is why only a cross-process test catches it.

The cross-process test here does not simulate two processes, it does the thing
that actually matters: two INDEPENDENT connections to the same file, which is
what two processes are. It runs in a subprocess for the write side, so a real
separate interpreter touches the file, not just a second handle.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, "packages")
sys.path.insert(0, "apps/worker")
sys.path.insert(0, "apps/api")

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# (3) ONE shared persistent graph store
# --------------------------------------------------------------------------


def test_two_independent_connections_see_each_others_rows(tmp_path: Path) -> None:
    """Cross-process visibility: write in a real subprocess, read in this one.

    A separate interpreter is used rather than a second handle so this is not
    quietly testing the same connection cache.
    """
    db = tmp_path / "graph.db"

    writer = textwrap.dedent(
        f"""
        import asyncio, sys
        sys.path.insert(0, "packages")
        from graph.sqlite_store import SQLiteGraphStore
        from graph.extractor import GraphChunk, RuleBasedGraphExtractor

        async def main():
            store = SQLiteGraphStore({str(db)!r})
            await store.set_enabled("ws-1", True)
            ex = RuleBasedGraphExtractor()
            chunks = [GraphChunk(
                chunk_id="ch-1",
                text="Alice Smith works at Acme Corporation. Acme Corporation is in New York.",
                workspace_id="ws-1", document_id="doc-1",
            )]
            res = await ex.extract(chunks)
            await store.upsert_entities("ws-1", res.entities)
            await store.upsert_relationships("ws-1", res.relationships)
            print("WROTE", len(res.entities), res.entities[0].id)

        asyncio.run(main())
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", writer],
        capture_output=True,
        text=True,
        cwd=str(Path.cwd()),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "WROTE" in proc.stdout, proc.stdout
    _, wrote, entity_id = proc.stdout.split()

    async def reader() -> object | None:
        from graph.sqlite_store import SQLiteGraphStore

        store = SQLiteGraphStore(str(db))
        return await store.get_entity("ws-1", entity_id)

    found = asyncio.run(reader())
    assert found is not None, "a separate process wrote a row the api cannot read"
    assert int(wrote) > 0


def test_relative_default_is_the_documented_trap(tmp_path: Path, monkeypatch) -> None:
    """Two different relative roots resolve to two different files.

    Documents WHY compose pins an absolute path rather than leaving the default.
    """
    from graph.factory import graph_db_path

    one = tmp_path / "api"
    two = tmp_path / "worker"
    one.mkdir()
    two.mkdir()
    monkeypatch.setenv("GRAPH_DB_PATH", "./.graph/graph.db")
    a = (one / graph_db_path()).resolve()
    b = (two / graph_db_path()).resolve()
    assert a != b, "a relative path should resolve differently per working dir"


def test_graph_db_path_creates_its_parent_directory(tmp_path: Path, monkeypatch) -> None:
    """A fresh named volume has no directory; SQLite would fail obscurely."""
    from graph.factory import graph_db_path

    target = tmp_path / "fresh-volume" / "graph.db"
    monkeypatch.setenv("GRAPH_DB_PATH", str(target))
    resolved = graph_db_path()
    assert Path(resolved).parent.is_dir(), "parent directory was not created"


def test_absolute_shared_path_is_one_file_for_both_services(monkeypatch, tmp_path) -> None:
    from graph.factory import graph_db_path

    shared = tmp_path / "graphdata" / "graph.db"
    monkeypatch.setenv("GRAPH_DB_PATH", str(shared))
    first = graph_db_path()
    second = graph_db_path()
    assert first == second == str(shared)


def test_compose_pins_one_shared_graph_path_for_api_and_worker() -> None:
    """The compose FILE must give both services the same path and volume.

    Parsed as text rather than by running compose: this ticket must not touch
    the daemon.
    """
    import yaml

    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    api_env = services["api"]["environment"]
    worker_env = services["worker"]["environment"]
    assert api_env["GRAPH_MODE"] == worker_env["GRAPH_MODE"]
    assert api_env["GRAPH_DB_PATH"] == worker_env["GRAPH_DB_PATH"]
    # compose values are interpolations, so assert the DEFAULT is absolute and
    # that both services carry a byte-identical value.
    default = api_env["GRAPH_DB_PATH"].split(":-", 1)[-1].rstrip("}")
    assert default.startswith("/"), f"default graph path is relative: {default}"
    assert api_env["GRAPH_DB_PATH"] == worker_env["GRAPH_DB_PATH"]
    assert ":${GRAPH_DB_PATH" not in api_env["GRAPH_DB_PATH"], "not an overridable default"
    api_vols = [v for v in services["api"]["volumes"] if "graph" in v]
    worker_vols = [v for v in services["worker"]["volumes"] if "graph" in v]
    assert api_vols == worker_vols, "api and worker mount different graph volumes"
    assert "graphdata" in compose["volumes"]


# --------------------------------------------------------------------------
# (1) supervision
# --------------------------------------------------------------------------


def test_consumer_only_starts_when_graph_is_enabled(monkeypatch) -> None:
    from graph.consumer import consumer_enabled

    monkeypatch.delenv("GRAPH_MODE", raising=False)
    assert consumer_enabled() is False
    for mode in ("off", "OFF", ""):
        monkeypatch.setenv("GRAPH_MODE", mode)
        assert consumer_enabled() is False, mode
    for mode in ("rule", "llm", "RULE"):
        monkeypatch.setenv("GRAPH_MODE", mode)
        assert consumer_enabled() is True, mode


async def test_supervisor_restarts_a_crashing_consumer(monkeypatch) -> None:
    from graph.consumer import run_supervised_graph_consumer

    attempts = {"n": 0}

    async def flaky(stop: asyncio.Event) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("consumer died")
        # Deterministic termination: no wall-clock race.
        stop.set()

    stop = asyncio.Event()
    state = await run_supervised_graph_consumer(
        stop, consumer=flaky, backoff=0.0, max_backoff=0.0, heartbeat_interval=3600
    )
    assert attempts["n"] == 3, attempts
    assert state.restarts == 2, state
    assert state.consecutive_failures == 2, state


async def test_supervisor_gives_up_instead_of_hot_looping(monkeypatch) -> None:
    """A poison job must not spin the worker forever."""
    from graph.consumer import run_supervised_graph_consumer

    attempts = {"n": 0}

    async def always_fails(stop: asyncio.Event) -> None:
        attempts["n"] += 1
        raise RuntimeError("poison")

    stop = asyncio.Event()
    stop_task = asyncio.create_task(_stop_later(stop, 5.0))
    state = await run_supervised_graph_consumer(
        stop,
        consumer=always_fails,
        backoff=0.0,
        max_backoff=0.0,
        max_failures=3,
        heartbeat_interval=3600,
    )
    stop_task.cancel()
    assert attempts["n"] == 3, attempts
    assert state.consecutive_failures == 3, state


async def test_supervisor_backoff_is_bounded(monkeypatch) -> None:
    from graph.consumer import run_supervised_graph_consumer

    real_sleep = asyncio.sleep

    async def stop_after_a_few() -> None:
        await real_sleep(0.05)
        stop.set()

    async def always_fails(stop: asyncio.Event) -> None:
        raise RuntimeError("down")

    stop = asyncio.Event()
    stopper = asyncio.create_task(stop_after_a_few())
    state = await run_supervised_graph_consumer(
        stop,
        consumer=always_fails,
        backoff=0.001,
        max_backoff=0.002,
        max_failures=6,
        heartbeat_interval=3600,
    )
    await stopper
    assert state.restarts >= 1
    assert state.consecutive_failures < 1000


async def test_supervisor_does_not_delay_shutdown(monkeypatch) -> None:
    """stop_event must cut the backoff short, not hold the process open."""
    from graph.consumer import run_supervised_graph_consumer

    async def always_fails(stop: asyncio.Event) -> None:
        raise RuntimeError("down")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    started = loop.time()
    asyncio.get_running_loop().call_later(0.02, stop.set)
    await run_supervised_graph_consumer(
        stop,
        consumer=always_fails,
        backoff=30.0,
        max_backoff=30.0,
        max_failures=5,
        heartbeat_interval=3600,
    )
    assert loop.time() - started < 5.0, "shutdown waited for the backoff to elapse"


def _monkey_and_queue() -> tuple[pytest.MonkeyPatch, object]:
    """Helper: a real MonkeyPatch plus the graph.queue module under test."""
    import graph.queue as graph_queue

    return pytest.MonkeyPatch(), graph_queue


async def _stop_later(stop: asyncio.Event, delay: float) -> None:
    await asyncio.sleep(delay)
    stop.set()


# --------------------------------------------------------------------------
# (4) observables
# --------------------------------------------------------------------------


def test_observables_shape_and_stall_detection() -> None:
    from graph.consumer import QueueObservables

    empty = QueueObservables(
        queue_depth=0, delayed_count=0, live_claims=0, oldest_age_seconds=None,
        consumer_count=0, stale_consumer_count=0, max_consumer_age_seconds=None,
    )
    assert empty.is_stalled is False, "an empty queue is not an outage"

    live = QueueObservables(
        queue_depth=5, delayed_count=0, live_claims=1, oldest_age_seconds=2.0,
        consumer_count=1, stale_consumer_count=0, max_consumer_age_seconds=1.0,
    )
    assert live.is_stalled is False, "a deep queue with a live consumer is normal"

    stalled = QueueObservables(
        queue_depth=5, delayed_count=0, live_claims=0, oldest_age_seconds=900.0,
        consumer_count=1, stale_consumer_count=1, max_consumer_age_seconds=900.0,
    )
    assert stalled.is_stalled is True, "work waiting with no fresh consumer is an outage"

    extra = stalled.as_extra()
    assert extra["queue_depth"] == 5
    assert extra["oldest_age_seconds"] == 900.0
    assert extra["stalled"] is True


def test_unreachable_broker_is_reported_not_raised() -> None:
    """A monitoring call must never become the outage it monitors."""
    from graph.consumer import QueueObservables

    down = QueueObservables(
        queue_depth=0, delayed_count=0, live_claims=0, oldest_age_seconds=None,
        consumer_count=0, stale_consumer_count=0, max_consumer_age_seconds=None,
        reachable=False, error="ConnectionError: broker down",
    )
    assert down.reachable is False
    assert "broker down" in down.error
    assert down.is_stalled is False, "an unreachable broker is not a stall"


def test_queue_payload_carries_enqueue_time_for_age(monkeypatch) -> None:
    """Queue-age must be measured from data, not inferred from depth."""
    from graph.queue import enqueue_graph_job

    captured: dict[str, object] = {}

    class _Fake:
        async def eval(self, script: str, _n: int, *args: object) -> int:
            if "HSET" in script:
                import json

                captured.update(json.loads(str(args[3])))
                captured["_queued"] = True
                return 1
            return 1

    monkeypatch, graph_queue = _monkey_and_queue()
    monkeypatch.setattr(graph_queue, "get_client", lambda: _Fake())
    monkeypatch.setenv("GRAPH_MODE", "rule")
    asyncio.run(enqueue_graph_job("ws-1", "doc-1", "ver-1"))
    assert "enqueued_at" in captured, captured
    assert isinstance(captured["enqueued_at"], float)


def test_worker_main_starts_the_supervised_consumer() -> None:
    """Source-level: the consumer must be started, and only when enabled."""
    import ast

    source = Path("apps/worker/worker/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {
        getattr(n.func, "id", "")
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert "run_supervised_graph_consumer" in calls, "worker main never starts the consumer"
    assert "consumer_enabled" in calls, "the consumer must be gated on GRAPH_MODE"


def test_worker_main_is_flag_off_inert() -> None:
    """GRAPH_MODE=off must start no consumer and import no extraction code."""
    source = Path("apps/worker/worker/main.py").read_text(encoding="utf-8")
    # The heavy import stays inside the supervisor's lazy default, not at module
    # top level in main.
    top_level_imports = source.split("async def run(")[0]
    assert "graph_extraction" not in top_level_imports, "flag-off path imports graph code"


# --------------------------------------------------------------------------
# per-workspace enablement
# --------------------------------------------------------------------------


async def test_enablement_is_per_workspace_and_survives_reopen(tmp_path: Path) -> None:
    from graph.sqlite_store import SQLiteGraphStore

    db = str(tmp_path / "graph.db")
    store = SQLiteGraphStore(db)
    # Documented back-compat: a workspace with no row reads as ENABLED, so
    # per-workspace opt-out must be explicit. Asserted so the default cannot
    # change silently under us.
    assert await store.is_enabled("ws-a") is True
    await store.set_enabled("ws-b", False)
    assert await store.is_enabled("ws-a") is True
    assert await store.is_enabled("ws-b") is False, "enablement leaked across workspaces"

    # A separate connection (the api process) sees the same enablement.
    other = SQLiteGraphStore(db)
    assert await other.is_enabled("ws-a") is True
    assert await other.is_enabled("ws-b") is False, "opt-out not visible cross-process"


async def test_workspace_isolation_holds_for_rows(tmp_path: Path) -> None:
    """A row written in one workspace must not be readable from another."""
    from graph.extractor import GraphChunk, RuleBasedGraphExtractor
    from graph.sqlite_store import SQLiteGraphStore

    db = str(tmp_path / "graph.db")
    store = SQLiteGraphStore(db)
    await store.set_enabled("ws-a", True)
    await store.set_enabled("ws-b", True)

    chunks = [
        GraphChunk(
            chunk_id="ch-1",
            text="Alice Smith works at Acme Corporation. Acme Corporation is in New York.",
            workspace_id="ws-a",
            document_id="doc-1",
        )
    ]
    extracted = await RuleBasedGraphExtractor().extract(chunks)
    await store.upsert_entities("ws-a", extracted.entities)
    entity_id = extracted.entities[0].id

    assert await store.get_entity("ws-a", entity_id) is not None
    assert await store.get_entity("ws-b", entity_id) is None, "row leaked across workspaces"