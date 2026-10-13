"""T-V1-021 integration: crash-recovery proof on disposable services.

Proves T-V1-020's durable claim/ack with a *dead worker*, not a diagram: a
real ``run_worker`` subprocess (``tests/fixtures/scripts/t021_crash_worker.py``)
is SIGKILLed mid-stage at five points — after claim, during parse, during FTS
index, during embed, and at the publication gate — then a restarted worker must
reconcile every job to READY (or an explicit, fenced FAILED) with:

- **zero loss** — every enqueued job id ends terminal; the queue, claims
  zset, claim-token hash and delayed zset all drain to empty;
- **zero duplication** — store row/point counts never grow across a
  redelivery (T-V1-016 stable publication IDs re-write in place);
- **zero resurrection** — a duplicate delivery whose document was deleted or
  superseded mid-crash is fenced (T-V1-019) to FAILED and leaves no rows.

Isolation from the live worker: every key lives under a unique
``INGEST_QUEUE_PREFIX`` (``t021-<rand>:``) so the compose worker container on
the default prefix never claims these jobs and this test never claims its
``INGEST_QUEUE_LEASE_SECONDS=2`` is set **in the process environment only**
(never committed): the crashed worker's heartbeat stops at SIGKILL, the lease
expires within 2s, and the restarted worker's ``recover_expired`` sweep
redelivers — that window is what the timing columns below measure.

Skipped loudly when Postgres/Redis/Qdrant are not reachable (``make up``).
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Generator
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.integration

PASSWORD = "Correct-Horse-Battery-9"
LEASE_SECONDS = "2"  # env-only fast expiry (T-V1-020 seam); never committed
TERMINAL_TIMEOUT = 60.0
STAGE_TIMEOUT = 45.0

REPO_ROOT = Path(__file__).resolve().parents[2]
CRASH_WORKER = REPO_ROOT / "tests" / "fixtures" / "scripts" / "t021_crash_worker.py"

#: Unique per run so the live compose worker (default prefix) and this test
#: can never see each other's jobs. Computed at import (harmless — it is just
#: a constant); the *environment* that consumes it is set per-test by
#: ``_queue_namespace`` below, never at import time (T-V1-057).
QUEUE_PREFIX = f"t021-{uuid.uuid4().hex[:12]}:"

_MATRIX: list[RecoveryRow] = []


@pytest.fixture(scope="module", autouse=True)
def _queue_namespace() -> Generator[None, None, None]:
    """Install this module's queue namespace, then take it back out.

    T-V1-057: both assignments used to sit at module level, i.e. they ran the
    moment pytest *imported* this file — before a single test executed. Every
    test collected earlier in the same session therefore ran against a queue
    prefix it never expected, which is what turned two unrelated
    queue-namespacing assertions red in every full-suite run (they passed in
    isolation). Scoping the mutation to this module's tests keeps the
    isolation guarantee it exists for — the compose worker on the default
    prefix still never sees these jobs — without leaking into anyone else.

    The child worker does not rely on this: ``_CrashedWorker.start`` copies
    ``os.environ`` and then sets both keys explicitly, so it gets the same
    values whether or not the parent's environment is currently mutated.
    """
    keys = ("INGEST_QUEUE_PREFIX", "INGEST_QUEUE_LEASE_SECONDS")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ["INGEST_QUEUE_PREFIX"] = QUEUE_PREFIX
    os.environ["INGEST_QUEUE_LEASE_SECONDS"] = LEASE_SECONDS
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# --------------------------------------------------------------------------------------
# service probe + app client (same pattern as test_dedup_versions.py)
# --------------------------------------------------------------------------------------


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def client() -> Generator:
    if not (_port_open(5432) and _port_open(6379) and _port_open(6333)):
        pytest.skip("postgres/redis/qdrant not reachable; run `make up`")

    from app.db import run_migrations

    run_migrations()

    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def marker_dir() -> Generator[Path, None, None]:
    """Module-scoped dir holding each cycle's crash-worker markers and logs.

    Swept on teardown by default (T-V1-066): these used to be left behind
    forever, one per run, so ``$HOME/.tmp`` accumulated without bound. The
    sweep is scoped to *the dir this module created* — older ``t021-markers-*``
    dirs are diagnostic evidence, so deleting another run's markers is not this
    harness's call.

    **Escape hatch — ``T021_KEEP_MARKERS``** (T-V1-067). The sweep also deletes
    the crash worker's own ``crash-N.log`` / ``recovery-N.log``, and those logs
    are how T-V1-064's root cause was found, so a failure that needs diagnosis
    after the fact has nothing left to read. Set the variable to skip the sweep:

    * name: ``T021_KEEP_MARKERS``
    * truthy spellings: ``1``, ``true``, ``yes`` (case-insensitive, surrounding
      whitespace ignored); **default OFF** — accumulation was the bug, so the
      sweep stays the default and keeping markers is an explicit choice.
    * when to use: when a crash test fails and you need its worker logs —
      set it, re-run *that one failing test* attached, read the logs out of the
      printed path, then unset it. Leaving it set will re-accumulate a dir per
      run, which is exactly what this fixture exists to prevent.

    The surviving dir's path is printed so it is findable without guessing::

        T021_KEEP_MARKERS=1 pytest tests/integration/test_crash_recovery.py::test_...
    """
    path = Path(tempfile.mkdtemp(prefix="t021-markers-", dir=str(Path.home() / ".tmp")))
    try:
        yield path
    finally:
        # if/else, not an early `return`: a `return` inside a generator's
        # `finally` swallows any exception pytest throws in at teardown.
        keep = os.environ.get("T021_KEEP_MARKERS", "").strip().lower() in {"1", "true", "yes"}
        if keep:
            print(f"t021: T021_KEEP_MARKERS set — keeping markers at {path}")
        else:
            # T-V1-066: sweep the dir this module created.
            import shutil

            shutil.rmtree(path, ignore_errors=True)


def _auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _new_user(client) -> str:
    email = f"t021-{uuid.uuid4().hex[:10]}@example.test"
    registered = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _create_workspace(client, token: str) -> str:
    response = client.post("/v1/workspaces", json={"name": "CrashRecovery"}, headers=_auth(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(client, token: str, workspace_id: str, filename: str, data: bytes) -> dict:
    response = client.post(
        f"/v1/workspaces/{workspace_id}/documents",
        files={"file": (filename, data, "text/plain")},
        headers=_auth(token),
    )
    assert response.status_code in (200, 201, 202), response.text
    return response.json()


def _delete_document(client, token: str, workspace_id: str, document_id: str) -> None:
    response = client.delete(
        f"/v1/workspaces/{workspace_id}/documents/{document_id}", headers=_auth(token)
    )
    assert response.status_code in (204, 207), response.text


# --------------------------------------------------------------------------------------
# store-authoritative observations (Postgres + live Qdrant scroll)
# --------------------------------------------------------------------------------------


def _db_scalar(query: str, params: dict):
    import asyncio

    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async def run():
        async with get_session_factory()() as session:
            return (await session.execute(text(query), params)).scalar_one()

    return asyncio.run(run())


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def _job_row(job_id: str) -> dict:
    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async def run() -> dict:
        async with get_session_factory()() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, stage, attempts, error_code, finished_at "
                        "FROM ingestion_jobs WHERE id = :i"
                    ),
                    {"i": job_id},
                )
            ).mappings().one()
            return dict(row)

    return asyncio_run(run())


def _chunk_count(version_id: str) -> int:
    return int(
        _db_scalar(
            "SELECT count(*) FROM chunks WHERE document_version_id = :v", {"v": version_id}
        )
    )


def _fts_version_count(version_id: str) -> int:
    return int(
        _db_scalar(
            "SELECT count(*) FROM lexical_docs WHERE document_version_id = :v", {"v": version_id}
        )
    )


def _qdrant_version_points(version_id: str) -> int:
    """Live Qdrant scroll (not SQL) — the vector-store side of every claim."""
    from qdrant_client.http.models import FieldCondition, Filter, MatchValue
    from retrieval.adapters.qdrant_store import QdrantVectorStore

    async def run() -> int:
        store = QdrantVectorStore(collection="knowledge")
        try:
            points, _ = await store._client.scroll(
                collection_name="knowledge",
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key="document_version_id", match=MatchValue(value=version_id)
                        )
                    ]
                ),
                limit=1000,
                with_payload=False,
                with_vectors=False,
            )
            return len(points)
        finally:
            await store.close()

    return asyncio_run(run())


def _stores(version_id: str) -> dict[str, int]:
    return {
        "chunks": _chunk_count(version_id),
        "fts": _fts_version_count(version_id),
        "points": _qdrant_version_points(version_id),
    }


# --------------------------------------------------------------------------------------
# queue observations (shared Redis, this run's namespaced keys only)
# --------------------------------------------------------------------------------------


def _redis():
    import redis as redis_sync

    return redis_sync.Redis.from_url("redis://localhost:6379/0", decode_responses=True)


def _queue_keys() -> dict[str, str]:
    return {
        "queue": f"ingest:{QUEUE_PREFIX}queue",
        "delayed": f"ingest:{QUEUE_PREFIX}delayed",
        "claims": f"ingest:{QUEUE_PREFIX}claims",
        "tokens": f"ingest:{QUEUE_PREFIX}claim_tokens",
    }


def _claim_present(job_id: str) -> bool:
    with _redis() as r:
        return r.zscore(_queue_keys()["claims"], job_id) is not None


def _queue_drained(timeout: float = 10.0) -> bool:
    """Queue/claims/tokens/delayed all empty (poll: the terminal job row
    commits just *before* the worker's final ack lands)."""
    keys = _queue_keys()
    deadline = time.time() + timeout
    while True:
        with _redis() as r:
            counts = (
                r.llen(keys["queue"]),
                r.zcard(keys["claims"]),
                r.hlen(keys["tokens"]),
                r.zcard(keys["delayed"]),
            )
        if counts == (0, 0, 0, 0):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.1)


def _cleanup_queue_keys() -> None:
    keys = list(_queue_keys().values())
    with _redis() as r:
        r.delete(*keys)


# --------------------------------------------------------------------------------------
# killable worker subprocess
# --------------------------------------------------------------------------------------

#: T-V1-054 — every worker this module spawns, for deterministic teardown.
#: A stalled worker waits on an Event forever, so "the test finished" and "the
#: worker died" are unrelated unless we make them related.
_SPAWNED: list[_CrashWorker] = []

#: How long to wait for a killed group to actually disappear before escalating.
_REAP_GRACE_SECONDS = 5.0
#: T-V1-066 — per-cycle handover. A stage that never fires makes
#: ``wait_stalled()`` raise *before* ``crash.kill()``, so that worker used to
#: stay alive into the next cycle, still holding the shared module prefix, and
#: could claim the next cycle's job. Each new cycle now reaps the previous ones
#: and polls until they are verifiably gone before spawning.
_CYCLE_GONE_TIMEOUT = 10.0
_CYCLE_POLL_INTERVAL = 0.1
#: The bracket keeps the pattern from matching this file's own pgrep argv.
_ORPHAN_PATTERN = "t021_crash[_]worker"


def _live_orphan_pids() -> list[int]:
    """PIDs of any ``t021_crash_worker`` still alive on this box (a real pgrep)."""
    result = subprocess.run(
        ["pgrep", "-f", _ORPHAN_PATTERN],
        capture_output=True,
        text=True,
        check=False,
    )
    return [int(line) for line in result.stdout.split() if line.strip().isdigit()]


def _reap_all_workers() -> None:
    """Kill every spawned worker's **process group** and wait for it to go.

    Group kill, not a best-effort ``terminate``: the workers are started with
    ``start_new_session=True`` so each leads its own group, which means one
    ``killpg`` reaches the worker *and anything it started*, and — unlike
    signalling a bare PID — cannot leave a grandchild wedged on a claim.
    """
    for worker in list(_SPAWNED):
        proc = worker.proc
        if proc is None:
            continue
        if proc.poll() is not None:
            continue
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=_REAP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL ignored
            pass


def _await_workers_gone(timeout: float = _CYCLE_GONE_TIMEOUT) -> bool:
    """Poll until every tracked worker is verifiably gone. Returns True if clean.

    A **poll**, not a blanket sleep: each tick re-reads ``proc.poll()`` so we
    return the instant the last worker is actually gone, and the bound is short
    because a surviving process here is a bug to report, not a condition to
    wait out.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = [w for w in _SPAWNED if w.proc is not None and w.proc.poll() is None]
        if not alive:
            return True
        time.sleep(_CYCLE_POLL_INTERVAL)
    return not [w for w in _SPAWNED if w.proc is not None and w.proc.poll() is None]


@pytest.fixture(scope="session", autouse=True)
def _no_orphan_workers() -> Generator[None, None, None]:
    """T-V1-054: after this module runs, not one crash worker may remain.

    Two independent nets, because each covers the other's blind spot:

    * this teardown (reap + assert) — the normal case;
    * ``atexit`` + the worker's own parent-watchdog — the case the card was
      written for, where the test process is SIGKILLed and *no* teardown runs
      at all.
    """
    baseline = set(_live_orphan_pids())
    try:
        yield
    finally:
        _reap_all_workers()
        stragglers = [pid for pid in _live_orphan_pids() if pid not in baseline]
        assert stragglers == [], (
            "orphaned t021_crash_worker processes survived the suite "
            f"(pids={stragglers}); they hold a claim and ~140MB RSS each"
        )


atexit.register(_reap_all_workers)


class _CrashWorker:
    """A real ``run_worker`` process that stalls at one stage until SIGKILLed."""

    def __init__(self, name: str, marker_dir: Path, crash_stage: str | None) -> None:
        self.name = name
        self.marker_dir = marker_dir
        self.crash_stage = crash_stage
        self.log_path = marker_dir / f"{name}.log"
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0

    def start(self) -> None:
        # T-V1-066 — per-cycle handover. Every cycle in this module shares one
        # QUEUE_PREFIX, so a worker left over from a previous cycle can claim
        # the *next* cycle's job and starve its stall seam (T-V1-064's
        # unpinned hypothesis, now pinned: a stage that never fires aborts the
        # scenario *before* ``crash.kill()``, so the worker really does survive
        # into the following cycle). Reap and poll-verify before spawning, so
        # each cycle starts from a provably empty worker set.
        if _SPAWNED:
            _reap_all_workers()
            if not _await_workers_gone():
                stragglers = [w.name for w in _SPAWNED if w.proc and w.proc.poll() is None]
                print(f"WARNING t021: workers still alive after wait: {stragglers}")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(REPO_ROOT / "packages"), str(REPO_ROOT / "apps" / "worker")]
        )
        env["INGEST_QUEUE_PREFIX"] = QUEUE_PREFIX
        env["INGEST_QUEUE_LEASE_SECONDS"] = LEASE_SECONDS
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["T021_MARKER_DIR"] = str(self.marker_dir)
        # T-V1-054: let the worker police itself if this test process dies
        # before its own teardown ever runs.
        env["T021_PARENT_PID"] = str(os.getpid())
        env.setdefault("MIGRATIONS_ON_BOOT", "false")
        env.setdefault("LOG_JSON", "false")
        env.setdefault("APP_ENV", "test")
        env.setdefault("TMPDIR", str(Path.home() / ".tmp"))
        if self.crash_stage:
            env["T021_CRASH_STAGE"] = self.crash_stage
        else:
            env.pop("T021_CRASH_STAGE", None)
        log = open(self.log_path, "ab")
        try:
            self.proc = subprocess.Popen(
                [sys.executable, str(CRASH_WORKER)],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                # T-V1-054: our own session/process group. This is what makes
                # the teardown's killpg deterministic — without it the worker
                # shares the pytest group and killpg would kill the test runner
                # too (and a dead parent leaves the children behind).
                start_new_session=True,
            )
            # Registered in a finally: a worker that spawned but then failed to
            # come up is still ours to reap.
            _SPAWNED.append(self)
        finally:
            log.close()
        self.started_at = time.time()

    @property
    def marker_path(self) -> Path:
        assert self.crash_stage is not None
        return self.marker_dir / f"{self.crash_stage}.stalled"

    def wait_stalled(self, timeout: float = STAGE_TIMEOUT) -> float:
        """Block until the worker announces the crash point; return elapsed s."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.marker_path.exists():
                return time.time() - self.started_at
            assert self.proc is not None and self.proc.poll() is None, (
                f"{self.name} exited (rc={self.proc.poll() if self.proc else '?'}) before "
                f"reaching stage {self.crash_stage!r}; log:\n{self.log_text()}"
            )
            time.sleep(0.05)
        raise AssertionError(f"{self.name} never reached stage {self.crash_stage!r}")

    def _killpg(self, sig: int) -> None:
        """Signal the worker's whole process group (T-V1-054)."""
        proc = self.proc
        assert proc is not None
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            # Already gone, or the group moved: fall back to the bare PID so the
            # caller's intent (this worker must be dead) is still honoured.
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

    def kill(self) -> float:
        """SIGKILL (a real crash: no finally, no ack, no lease release).

        Group-killed rather than bare-PID-killed so that anything the worker
        started dies with it. SIGKILL still bypasses every Python-level cleanup,
        which is the whole point of this scenario.
        """
        assert self.proc is not None
        t_kill = time.time()
        self._killpg(signal.SIGKILL)
        self.proc.wait(timeout=_REAP_GRACE_SECONDS)
        return t_kill

    def stop(self) -> None:
        """Graceful stop for the recovery worker (drain one loop, then kill)."""
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self._killpg(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._killpg(signal.SIGKILL)
                try:
                    self.proc.wait(timeout=_REAP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    pass

    def log_text(self) -> str:
        if not self.log_path.exists():
            return "<no log>"
        return self.log_path.read_text(errors="replace")[-4000:]


# --------------------------------------------------------------------------------------
# matrix row
# --------------------------------------------------------------------------------------


@dataclass
class RecoveryRow:
    scenario: str
    stage: str
    job_id: str
    version_id: str
    stall_after_s: float
    state_at_kill: dict
    claim_alive_at_kill: bool
    claim_survives_kill: bool
    stores_at_kill: dict
    kill_to_redeliver_s: float | None
    kill_to_terminal_s: float
    final_status: str
    final_attempts: int
    final_error_code: str | None
    stores_final: dict
    store_growth: dict
    queue_drained: bool
    verdict: str = "PASS"
    notes: list[str] = field(default_factory=list)


def _wait_terminal(
    job_id: str, *, attempts_at_kill: int, status_at_kill: str, timeout: float = TERMINAL_TIMEOUT
) -> tuple[dict, float | None, float]:
    """Poll the job row until terminal; return (row, redelivered_at, terminal_at).

    "Redelivered" is store-authoritative: the first observation where the
    recovery worker touched the job (attempts or status moved off the
    at-kill snapshot) — no log parsing, no sleeps-as-synchronization.
    """
    t0 = time.time()
    redelivered_at: float | None = None
    while True:
        row = _job_row(job_id)
        now = time.time()
        if redelivered_at is None and (
            row["attempts"] > attempts_at_kill or row["status"] != status_at_kill
        ):
            redelivered_at = now
        if row["status"] in ("ready", "failed"):
            return row, redelivered_at, now
        assert now - t0 < timeout, (
            f"job {job_id} not terminal after {timeout}s (status={row['status']!r} "
            f"stage={row['stage']!r} attempts={row['attempts']})"
        )
        time.sleep(0.25)


def _run_crash_cycle(
    *,
    scenario: str,
    stage: str,
    client,
    token: str,
    workspace_id: str,
    filename: str,
    payload: bytes,
    marker_dir: Path,
    worker_counter: list[int],
    pre_kill_hook=None,
) -> tuple[RecoveryRow, dict]:
    """enqueue → crash-mid-stage → SIGKILL → restart → reconcile.

    Returns (matrix row, final job row). ``pre_kill_hook`` runs between the
    kill and the restart (the fencing scenarios delete/replace the document
    while the dead worker's claim is still expiring).
    """
    worker_counter[0] += 1
    cycle_marker_dir = marker_dir / f"cycle-{worker_counter[0]}-{stage}"
    cycle_marker_dir.mkdir(parents=True, exist_ok=True)
    uploaded = _upload(client, token, workspace_id, filename, payload)
    job_id, version_id = uploaded["job_id"], uploaded["version_id"]
    assert uploaded["status"] == "queued"

    crash = _CrashWorker(f"crash-{worker_counter[0]}", cycle_marker_dir, stage)
    crash.start()
    stall_after_s = crash.wait_stalled()

    state_at_kill = _job_row(job_id)
    claim_alive_at_kill = _claim_present(job_id)
    stores_at_kill = _stores(version_id)

    t_kill = crash.kill()
    claim_survives_kill = _claim_present(job_id)
    assert claim_survives_kill, (
        f"[{scenario}] the claim vanished at SIGKILL — ack leaked before durability"
    )

    notes: list[str] = []
    if pre_kill_hook is not None:
        notes.extend(
            pre_kill_hook(job_id=job_id, version_id=version_id, document_id=uploaded["document_id"])
            or []
        )

    recovery = _CrashWorker(f"recovery-{worker_counter[0]}", marker_dir, None)
    recovery.start()
    try:
        final, redelivered_at, t_terminal = _wait_terminal(
            job_id,
            attempts_at_kill=int(state_at_kill["attempts"]),
            status_at_kill=str(state_at_kill["status"]),
        )
        # The ack follows the terminal commit by milliseconds — let the live
        # worker release its claim before we stop it, so the drained-keys
        # assertion tests the worker's ack, not our SIGTERM timing.
        drained = _queue_drained()
    finally:
        recovery.stop()

    final_stores = _stores(version_id)
    row = RecoveryRow(
        scenario=scenario,
        stage=stage,
        job_id=job_id,
        version_id=version_id,
        stall_after_s=round(stall_after_s, 2),
        state_at_kill={
            "status": state_at_kill["status"],
            "stage": state_at_kill["stage"],
            "attempts": state_at_kill["attempts"],
        },
        claim_alive_at_kill=claim_alive_at_kill,
        claim_survives_kill=claim_survives_kill,
        stores_at_kill=stores_at_kill,
        kill_to_redeliver_s=round(redelivered_at - t_kill, 2) if redelivered_at else None,
        kill_to_terminal_s=round(t_terminal - t_kill, 2),
        final_status=str(final["status"]),
        final_attempts=int(final["attempts"]),
        final_error_code=final["error_code"],
        stores_final=final_stores,
        store_growth={
            key: final_stores[key] - stores_at_kill[key] for key in ("chunks", "fts", "points")
        },
        queue_drained=drained,
        notes=notes,
    )
    _MATRIX.append(row)
    return row, final


def _assert_common_invariants(row: RecoveryRow) -> None:
    assert row.claim_alive_at_kill, f"[{row.scenario}] worker never held the claim at the kill"
    assert row.queue_drained, f"[{row.scenario}] queue keys did not drain: {row}"
    assert row.final_status in ("ready", "failed"), f"[{row.scenario}] job vanished mid-air"


# --------------------------------------------------------------------------------------
# the five crash stages (5/5)
# --------------------------------------------------------------------------------------


def test_after_claim_crash_is_redelivered(client, marker_dir) -> None:
    """Kill between claim and any pipeline work: pure queue-level recovery."""
    token = _new_user(client)
    ws = _create_workspace(client, token)
    row, _ = _run_crash_cycle(
        scenario="after_claim",
        stage="after_claim",
        client=client,
        token=token,
        workspace_id=ws,
        filename="after-claim.txt",
        payload=f"after claim probe {uuid.uuid4().hex} canary velvet".encode(),
        marker_dir=marker_dir,
        worker_counter=[0],
    )
    _assert_common_invariants(row)
    assert row.state_at_kill["status"] == "queued", row
    assert row.state_at_kill["attempts"] == 0, row
    assert row.final_status == "ready", row
    assert row.final_attempts == 1, row  # exactly one redelivery, no retry storm
    assert row.stores_at_kill == {"chunks": 0, "fts": 0, "points": 0}, row
    assert all(v >= 1 for v in row.stores_final.values()), row
    assert row.final_error_code is None, row


def test_crash_during_parse_is_redelivered(client, marker_dir) -> None:
    """Kill mid-parse: job row says running/parsing, nothing durable yet."""
    token = _new_user(client)
    ws = _create_workspace(client, token)
    row, _ = _run_crash_cycle(
        scenario="parse",
        stage="parse",
        client=client,
        token=token,
        workspace_id=ws,
        filename="parse.txt",
        payload=f"parse stage probe {uuid.uuid4().hex} marigold saffron".encode(),
        marker_dir=marker_dir,
        worker_counter=[1],
    )
    _assert_common_invariants(row)
    assert row.state_at_kill["status"] == "running", row
    assert row.state_at_kill["stage"] == "parsing", row
    assert row.final_status == "ready", row
    assert row.final_attempts == 2, row  # attempt 1 died mid-parse, attempt 2 finished
    assert row.stores_at_kill == {"chunks": 0, "fts": 0, "points": 0}, row
    assert all(v >= 1 for v in row.stores_final.values()), row


def test_crash_during_fts_index_is_redelivered(client, marker_dir) -> None:
    """Kill mid-index: chunk rows durable, FTS rows not — redelivery must not
    duplicate the chunk rows that already landed (stable IDs)."""
    token = _new_user(client)
    ws = _create_workspace(client, token)
    row, _ = _run_crash_cycle(
        scenario="index",
        stage="index",
        client=client,
        token=token,
        workspace_id=ws,
        filename="index.txt",
        payload=f"index stage probe {uuid.uuid4().hex} cypress laurel".encode(),
        marker_dir=marker_dir,
        worker_counter=[2],
    )
    _assert_common_invariants(row)
    assert row.state_at_kill["status"] == "running", row
    assert row.state_at_kill["stage"] == "indexing", row
    assert row.stores_at_kill["chunks"] >= 1, row  # chunk rows committed pre-crash
    assert row.stores_at_kill["fts"] == 0, row  # FTS transaction never committed
    assert row.final_status == "ready", row
    assert row.final_attempts == 2, row
    assert row.store_growth["chunks"] == 0, f"chunk rows duplicated across redelivery: {row}"
    assert row.stores_final["chunks"] == row.stores_final["fts"] == row.stores_final["points"], row


def test_crash_during_embed_is_redelivered(client, marker_dir) -> None:
    """Kill mid-embed: chunks + FTS durable, no vectors — redelivery completes
    the generation in place (FTS/chunk counts unchanged)."""
    token = _new_user(client)
    ws = _create_workspace(client, token)
    row, _ = _run_crash_cycle(
        scenario="embed",
        stage="embed",
        client=client,
        token=token,
        workspace_id=ws,
        filename="embed.txt",
        payload=f"embed stage probe {uuid.uuid4().hex} juniper obsidian".encode(),
        marker_dir=marker_dir,
        worker_counter=[3],
    )
    _assert_common_invariants(row)
    assert row.state_at_kill["stage"] == "embedding", row
    assert row.stores_at_kill["fts"] >= 1, row  # FTS committed pre-crash
    assert row.stores_at_kill["points"] == 0, row  # vectors never written
    assert row.final_status == "ready", row
    assert row.final_attempts == 2, row
    assert row.store_growth["chunks"] == 0, row
    assert row.store_growth["fts"] == 0, f"FTS rows duplicated across redelivery: {row}"
    assert row.stores_final["points"] >= 1, row


def test_crash_at_publication_gate_is_idempotent(client, marker_dir) -> None:
    """Kill at the pre-READY gate with EVERY store write already durable:
    redelivery rewrites the same stable IDs — zero store growth, then READY."""
    token = _new_user(client)
    ws = _create_workspace(client, token)
    row, _ = _run_crash_cycle(
        scenario="publication",
        stage="publication",
        client=client,
        token=token,
        workspace_id=ws,
        filename="publication.txt",
        payload=f"publication stage probe {uuid.uuid4().hex} basalt cobalt".encode(),
        marker_dir=marker_dir,
        worker_counter=[4],
    )
    _assert_common_invariants(row)
    assert row.state_at_kill["stage"] == "embedding", row  # last committed stage pre-READY
    assert all(v >= 1 for v in row.stores_at_kill.values()), row
    assert row.final_status == "ready", row
    assert row.final_attempts == 2, row
    assert row.store_growth == {"chunks": 0, "fts": 0, "points": 0}, (
        f"a redelivery of a fully-written generation grew a store: {row}"
    )
    assert row.final_error_code is None, row


# --------------------------------------------------------------------------------------
# duplicate delivery cannot publish deleted / superseded (reviewer checklist 3)
# --------------------------------------------------------------------------------------


def test_redelivery_after_delete_is_fenced_not_resurrected(client, marker_dir) -> None:
    """Crash mid-stage, DELETE the document while the claim expires, restart:
    the duplicate delivery must end FAILED (DOCUMENT_DELETED) and leave zero rows."""
    token = _new_user(client)
    ws = _create_workspace(client, token)

    def _delete_it(*, job_id: str, version_id: str, document_id: str) -> list[str]:
        _delete_document(client, token, ws, document_id)
        return [f"document {document_id} deleted between kill and restart"]

    row, final = _run_crash_cycle(
        scenario="fenced_delete",
        stage="embed",
        client=client,
        token=token,
        workspace_id=ws,
        filename="fenced-delete.txt",
        payload=f"fenced delete probe {uuid.uuid4().hex} tundra quartz".encode(),
        marker_dir=marker_dir,
        worker_counter=[5],
        pre_kill_hook=_delete_it,
    )
    _assert_common_invariants(row)
    assert row.final_status == "failed", row
    assert row.final_error_code == "DOCUMENT_DELETED", row
    assert str(final["error_code"]) == "DOCUMENT_DELETED", final  # row agrees with DB
    assert row.final_attempts == row.state_at_kill["attempts"] + 1, (
        f"a non-retryable fence must burn exactly one redelivery: {row}"
    )
    assert row.stores_final == {"chunks": 0, "fts": 0, "points": 0}, (
        f"a dead generation left rows behind (resurrection): {row}"
    )


def test_redelivery_after_replace_publishes_only_the_new_generation(client, marker_dir) -> None:
    """Crash v1 mid-stage, replace with v2 while the claim expires, restart:
    v1's duplicate delivery is fenced (STALE_GENERATION, zero rows), v2 READY,
    and live search exposes v2 only."""
    token = _new_user(client)
    ws = _create_workspace(client, token)
    v1_marker = f"v1stale{uuid.uuid4().hex[:8]}"
    v2_marker = f"v2fresh{uuid.uuid4().hex[:8]}"

    def _replace_it(*, job_id: str, version_id: str, document_id: str) -> list[str]:
        replaced = _upload(
            client, token, ws, "fenced-replace.txt", f"{v2_marker} replacement".encode()
        )
        assert replaced["version_id"] != version_id
        return [
            f"v1 {version_id} superseded by {replaced['version_id']} between kill and restart",
            f"v2 job {replaced['job_id']} queued alongside the dead claim",
        ]

    row, final = _run_crash_cycle(
        scenario="fenced_replace",
        stage="embed",
        client=client,
        token=token,
        workspace_id=ws,
        filename="fenced-replace.txt",
        payload=f"{v1_marker} original".encode(),
        marker_dir=marker_dir,
        worker_counter=[6],
        pre_kill_hook=_replace_it,
    )
    _assert_common_invariants(row)
    assert row.final_status == "failed", row
    assert row.final_error_code == "STALE_GENERATION", row
    assert str(final["status"]) == "failed", final  # row agrees with DB
    assert row.stores_final == {"chunks": 0, "fts": 0, "points": 0}, (
        f"superseded generation resurrected by its duplicate delivery: {row}"
    )

    # The surviving generation must be live and alone. v2's job was enqueued by
    # the replace and drained by the SAME recovery worker — but be explicit so
    # the assertion never depends on loop timing: run one more reconcile pass.
    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async def _latest_job_for_file() -> tuple[str, str, str]:
        async with get_session_factory()() as session:
            row_ = (
                await session.execute(
                    text(
                        "SELECT j.document_version_id, j.id, j.status "
                        "FROM ingestion_jobs j JOIN documents d ON d.id = j.document_id "
                        "WHERE d.source_filename = 'fenced-replace.txt' "
                        "AND d.workspace_id = :ws ORDER BY j.created_at DESC LIMIT 1"
                    ),
                    {"ws": ws},
                )
            ).one()
            return str(row_[0]), str(row_[1]), str(row_[2])

    v2_version, v2_job_id, v2_status = asyncio_run(_latest_job_for_file())
    if v2_status != "ready":
        reconcile = _CrashWorker("recovery-v2", marker_dir, None)
        reconcile.start()
        try:
            _, v2_status = _wait_v2_ready(v2_job_id)
        finally:
            reconcile.stop()
    assert v2_status == "ready", f"v2 job not READY after reconcile: {v2_status}"
    assert _stores(v2_version)["points"] >= 1

    results = client.post(
        f"/v1/workspaces/{ws}/search",
        json={"query": v2_marker, "mode": "keyword"},
        headers=_auth(token),
    ).json()["results"]
    assert results, "v2 content is not retrievable after the crash cycle"
    assert all(v2_marker in r["content"] for r in results), (
        f"search returned non-v2 evidence after the crash cycle: {results}"
    )

    stale = client.post(
        f"/v1/workspaces/{ws}/search",
        json={"query": v1_marker, "mode": "keyword"},
        headers=_auth(token),
    ).json()["results"]
    assert stale == [], f"stale v1 content still retrievable: {stale}"


def _wait_v2_ready(job_id: str, timeout: float = TERMINAL_TIMEOUT) -> tuple[dict, str]:
    t0 = time.time()
    while True:
        row = _job_row(job_id)
        if row["status"] in ("ready", "failed"):
            return row, str(row["status"])
        assert time.time() - t0 < timeout, f"v2 job {job_id} stuck at {row['status']!r}"
        time.sleep(0.25)


# --------------------------------------------------------------------------------------
# the matrix (runs last in this file)
# --------------------------------------------------------------------------------------


def test_zz_recovery_matrix_is_green(client, marker_dir) -> None:
    """Final gate: all five stages covered, no lost jobs, no duplicate growth,
    fenced redeliveries clean — then print the matrix for the handoff."""
    by_stage = {row.stage: row for row in _MATRIX}
    required = {"after_claim", "parse", "index", "embed", "publication"}
    covered = required & by_stage.keys()
    assert covered == required, f"missing crash stages: {required - covered}"

    failures = [row for row in _MATRIX if row.verdict != "PASS"]
    assert not failures, f"matrix rows failed: {failures}"

    for row in _MATRIX:
        _assert_common_invariants(row)
        if row.scenario.startswith("fenced_"):
            # Fenced rows end empty by design; the resurrection check is in-row.
            assert row.stores_final == {"chunks": 0, "fts": 0, "points": 0}, row
            continue
        assert row.final_status == "ready", row
        # Universal no-duplication invariant: every chunk landed in FTS and
        # Qdrant EXACTLY once (equal counts — a duplicate on any leg breaks
        # the equality; a lost write breaks it too).
        assert (
            row.stores_final["chunks"]
            == row.stores_final["fts"]
            == row.stores_final["points"]
        ), f"inconsistent store counts after recovery: {row}"
        if row.stage == "publication":
            # Everything was already durable at the kill — redelivery must be
            # a byte-for-byte no-op on all three stores (T-V1-016 stable IDs).
            assert row.store_growth == {"chunks": 0, "fts": 0, "points": 0}, (
                f"non-idempotent redelivery grew a store: {row}"
            )

    _cleanup_queue_keys()

    header = (
        f"{'scenario':<16}{'stage':<13}{'kill state':<20}{'stores@kill':<22}"
        f"{'redeliver s':<13}{'terminal s':<12}{'attempts':<10}{'final':<8}{'error':<20}"
    )
    lines = [
        "",
        "T-V1-021 RECOVERY MATRIX (live Postgres+Redis+Qdrant, lease=2s)",
        header,
        "-" * len(header),
    ]
    for row in _MATRIX:
        kill_state = f"{row.state_at_kill['status']}/{row.state_at_kill['stage']}"
        stores_kill = (
            f"c{row.stores_at_kill['chunks']}/f{row.stores_at_kill['fts']}"
            f"/p{row.stores_at_kill['points']}"
        )
        lines.append(
            f"{row.scenario:<16}{row.stage:<13}{kill_state:<20}{stores_kill:<22}"
            f"{row.kill_to_redeliver_s!s:<13}{row.kill_to_terminal_s!s:<12}"
            f"{row.final_attempts!s:<10}{row.final_status:<8}"
            f"{row.final_error_code!s:<20}"
        )
    lines.append("-" * len(header))
    lines.append(
        f"jobs: {len(_MATRIX)} | lost: 0 | duplicated store growth: 0 | "
        f"resurrections: 0 | stages covered: {len(required)}/5 | queue drained: yes"
    )
    report = "\n".join(lines)
    print(report)
    out = Path(tempfile.gettempdir()) / f"t021-matrix-{uuid.uuid4().hex[:8]}.json"
    out.write_text(json.dumps([asdict(r) for r in _MATRIX], indent=2, default=str))
    print(f"matrix json: {out}")
