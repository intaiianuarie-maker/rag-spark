"""T-R10 — graph queue reliability: typed failures and durable receipts.

The defect this closes, reproduced at HEAD before the fix:

  ``enqueue_graph_extraction`` caught the broker failure, logged a warning and
  ``return None`` — the SAME value it returns when graph is merely disabled. The
  caller therefore could not tell a clean skip from a queue outage, logged
  ``graph_job_enqueued``, and the document went READY with no graph job anything
  would ever claim. A false success, on a job the user believes is indexed.

Second defect, in ``graph.queue.enqueue_graph_job``: the payload and the queue
entry were written by two separate Redis commands. A crash between them leaves
either an orphaned payload nothing claims or a queued job id whose payload was
never stored. It also returned a job id without ever reading it back, so a
"successful" enqueue was not proof of durability.

No live broker is used: ``get_client`` is replaced with a fake that records
calls and can inject failures at either the write or the receipt.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, "packages")
sys.path.insert(0, "apps/worker")
sys.path.insert(0, "apps/api")

from redis.exceptions import ConnectionError as RedisConnectionError

pytestmark = pytest.mark.unit


class _FakeRedis:
    """Scratch broker. Records calls; can fail the write or the receipt.

    Models only what enqueue_graph_job uses: ``eval``. Deliberately NOT a redis
    emulator — if the production code grows a new command, this fake must grow
    with it rather than silently passing.
    """

    def __init__(self, *, fail_write: bool = False, receipt: int = 1) -> None:
        self.fail_write = fail_write
        self.receipt = receipt
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.payloads: dict[str, str] = {}
        self.queue: list[str] = []

    async def eval(self, script: str, _numkeys: int, *args: object) -> int:
        self.calls.append((script.strip().splitlines()[0], args))
        if "HSET" in script:
            if self.fail_write:
                raise RedisConnectionError("broker down")
            job_id, payload = str(args[2]), str(args[3])
            # Atomic: both structures move together or neither does.
            self.payloads[job_id] = payload
            self.queue.insert(0, job_id)
            return 1
        if "LPOS" in script:
            job_id = str(args[2])
            if job_id not in self.payloads or job_id not in self.queue:
                return 0
            return self.receipt
        raise AssertionError(f"unexpected script: {script[:40]}")

    def write_calls(self) -> int:
        return sum(1 for script, _ in self.calls if "HSET" in script)


def _install(monkeypatch, client: _FakeRedis) -> None:
    from graph import queue as graph_queue

    monkeypatch.setattr(graph_queue, "get_client", lambda: client)


def _enable(monkeypatch, mode: str = "rule") -> None:
    monkeypatch.setenv("GRAPH_MODE", mode)
    os.environ["GRAPH_MODE"] = mode


# --------------------------------------------------------------------------
# METRIC — broker failures logging false success: 0
# --------------------------------------------------------------------------


async def test_broker_failure_raises_and_returns_no_job_id(monkeypatch) -> None:
    """An outage must RAISE, not quietly hand back a falsy 'nothing to do'."""
    from graph.queue import GraphQueueUnavailable, enqueue_graph_job

    _enable(monkeypatch)
    broker = _FakeRedis(fail_write=True)
    _install(monkeypatch, broker)
    with pytest.raises(GraphQueueUnavailable):
        await enqueue_graph_job("ws-1", "doc-1", "ver-1")
    assert broker.payloads == {}, "a failed enqueue left a payload behind"
    assert broker.queue == [], "a failed enqueue queued a job nothing can claim"


async def test_enqueue_extraction_propagates_instead_of_swallowing(monkeypatch) -> None:
    """The exact false-success path, reproduced and closed.

    Before the fix this returned None, the caller logged graph_job_enqueued and
    the job went READY.
    """
    from jobs.graph_extraction import enqueue_graph_extraction

    _enable(monkeypatch)
    broker = _FakeRedis(fail_write=True)
    _install(monkeypatch, broker)
    with pytest.raises(Exception) as excinfo:
        await enqueue_graph_extraction("ws-1", "doc-1", "ver-1")
    assert "graph queue unavailable" in str(excinfo.value), excinfo.value
    assert broker.write_calls() >= 1, "the broker was never even attempted"


async def test_propagation_is_not_confused_with_the_disabled_skip(monkeypatch) -> None:
    """Graph-disabled returns None; a broken queue RAISES. Never both None."""
    from jobs.graph_extraction import enqueue_graph_extraction

    # 1. disabled -> clean skip, broker never touched
    _enable(monkeypatch, mode="off")
    broker = _FakeRedis(fail_write=True)
    _install(monkeypatch, broker)
    assert await enqueue_graph_extraction("ws-1", "doc-1", "ver-1") is None
    assert broker.calls == [], "a disabled graph still touched the broker"

    # 2. enabled but broken -> loud
    _enable(monkeypatch, mode="rule")
    with pytest.raises(Exception):  # noqa: B017
        await enqueue_graph_extraction("ws-1", "doc-1", "ver-1")


# --------------------------------------------------------------------------
# durable receipt
# --------------------------------------------------------------------------


async def test_enqueue_is_atomic_single_script(monkeypatch) -> None:
    """Payload + queue entry must be ONE script, not two commands."""
    from graph.queue import enqueue_graph_job

    _enable(monkeypatch)
    broker = _FakeRedis()
    _install(monkeypatch, broker)
    await enqueue_graph_job("ws-1", "doc-1", "ver-1")
    assert broker.write_calls() == 1, "write was split into multiple commands"


async def test_unconfirmed_enqueue_is_treated_as_failure(monkeypatch) -> None:
    """A write that the broker cannot confirm must NOT return a job id.

    This is the 'durable receipt required' rule: a caller that receives an id
    must be able to rely on a worker being able to claim it.
    """
    from graph.queue import GraphQueueUnavailable, enqueue_graph_job

    _enable(monkeypatch)
    broker = _FakeRedis(receipt=0)  # write "succeeds", read-back finds nothing
    _install(monkeypatch, broker)
    with pytest.raises(GraphQueueUnavailable, match="no durable job receipt"):
        await enqueue_graph_job("ws-1", "doc-1", "ver-1")


async def test_successful_enqueue_returns_a_confirmed_job_id(monkeypatch) -> None:
    from graph.queue import enqueue_graph_job

    _enable(monkeypatch)
    broker = _FakeRedis()
    _install(monkeypatch, broker)
    job_id = await enqueue_graph_job("ws-1", "doc-1", "ver-1")
    assert job_id in broker.payloads, "returned an id with no stored payload"
    assert job_id in broker.queue, "returned an id that is not in the queue"


# --------------------------------------------------------------------------
# replay / idempotency
# --------------------------------------------------------------------------


async def test_replay_produces_independent_confirmed_jobs(monkeypatch) -> None:
    """Replaying an enqueue must not corrupt the queue or lose a payload.

    Each replay gets its own id and its own verified receipt; nothing is
    deduplicated silently, because extraction idempotency lives at the store
    layer (deterministic node ids), not here.
    """
    from graph.queue import enqueue_graph_job

    _enable(monkeypatch)
    broker = _FakeRedis()
    _install(monkeypatch, broker)
    first = await enqueue_graph_job("ws-1", "doc-1", "ver-1")
    second = await enqueue_graph_job("ws-1", "doc-1", "ver-1")
    assert first != second
    assert set(broker.queue) == {first, second}
    assert set(broker.payloads) == {first, second}
    assert len(broker.queue) == len(broker.payloads), "orphan or double-queued entry"


async def test_replay_after_a_failure_recovers_cleanly(monkeypatch) -> None:
    """A retry after an outage leaves no debris from the failed attempt."""
    from graph.queue import enqueue_graph_job

    _enable(monkeypatch)
    broken = _FakeRedis(fail_write=True)
    _install(monkeypatch, broken)
    with pytest.raises(Exception):  # noqa: B017
        await enqueue_graph_job("ws-1", "doc-1", "ver-1")
    assert broken.payloads == {} and broken.queue == []

    healthy = _FakeRedis()
    _install(monkeypatch, healthy)
    job_id = await enqueue_graph_job("ws-1", "doc-1", "ver-1")
    assert job_id in healthy.queue and job_id in healthy.payloads


# --------------------------------------------------------------------------
# taxonomy close
# --------------------------------------------------------------------------


def test_graph_enqueue_failed_is_a_registered_reason_code() -> None:
    from core.errors import ERROR_REGISTRY, ReasonCode, canonical_code

    assert ReasonCode.GRAPH_ENQUEUE_FAILED.value == "GRAPH_ENQUEUE_FAILED"
    assert ReasonCode.GRAPH_ENQUEUE_FAILED in ERROR_REGISTRY
    assert canonical_code("GRAPH_ENQUEUE_FAILED") is ReasonCode.GRAPH_ENQUEUE_FAILED


def test_graph_enqueue_failed_is_retryable_and_job_visible() -> None:
    """Retryable, and valid on a FAILED job — the pipeline raises it there."""
    from core.errors import ERROR_REGISTRY, JOB_STATUS_REASON_CODES, JobStatus, ReasonCode

    spec = ERROR_REGISTRY[ReasonCode.GRAPH_ENQUEUE_FAILED]
    assert spec.retryable is True, "a broker outage must be retryable"
    assert spec.http_status == 503
    assert ReasonCode.GRAPH_ENQUEUE_FAILED in JOB_STATUS_REASON_CODES[JobStatus.FAILED]


def test_pipeline_raises_the_registered_code() -> None:
    """The literal the ingest pipeline raises must be the registered one."""
    import pathlib
    import re

    from core.errors import canonical_code

    source = pathlib.Path("apps/worker/jobs/ingest_pipeline.py").read_text(encoding="utf-8")
    literals = set(re.findall(r'IngestPipelineError\(\s*\n?\s*"([A-Za-z_]+)"', source))
    assert "GRAPH_ENQUEUE_FAILED" in literals
    assert all(canonical_code(raw) is not None for raw in literals), sorted(literals)


def test_graph_layer_defines_no_parallel_taxonomy() -> None:
    """Mirrors the contract rule: the graph package owns no reason codes.

    Enforced here too because the failure mode I fixed was a graph-layer
    exception class standing in for a taxonomy code.
    """
    import pathlib
    import re

    root = pathlib.Path("packages/graph")
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"class\s+\w*(Reason|Error)Code\w*\s*\(", text):
            offenders.append(f"{path.name}: local reason-code class")
        if re.search(r'reason_code\s*=\s*"[A-Z_]{4,}"', text):
            offenders.append(f"{path.name}: raw reason-code literal")
    assert offenders == [], offenders

def test_the_graph_enqueue_happens_before_the_publish() -> None:
    """A raise is only safe if nothing READY has already been written.

    The original bug was not that the pipeline failed to raise — it DID raise —
    but that the swallow upstream meant the raise never fired, so the code
    carried on to the publish. This pins the ordering that makes the raise
    meaningful: the enqueue block must come before the publish statement.
    """
    import ast
    import pathlib

    source = pathlib.Path("apps/worker/jobs/ingest_pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if func == "enqueue_graph_extraction":
                lines["enqueue"] = min(lines.get("enqueue", node.lineno), node.lineno)
            if func == "publish_job_if_generation_live":
                lines["publish"] = min(lines.get("publish", node.lineno), node.lineno)
    assert lines["enqueue"] is not None, "pipeline no longer enqueues graph extraction"
    assert lines["publish"] is not None, "pipeline no longer publishes"
    assert lines["enqueue"] < lines["publish"], (
        "graph enqueue must precede the publish, or a raised enqueue failure "
        "could not prevent a READY write"
    )
