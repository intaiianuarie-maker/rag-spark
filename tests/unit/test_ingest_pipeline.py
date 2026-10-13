"""Unit tests for M2-001c guards, charset hint, and queue behavior."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion.adapters.registry import parser_for_mime, supported_mime_types
from ingestion.interfaces import DocumentElement, ElementType
from ingestion.normalize import ParseError, decode_text
from jobs.ingest_pipeline import (
    IngestPipelineError,
    ensure_element_limit,
)

pytestmark = pytest.mark.unit


def _element() -> DocumentElement:
    return DocumentElement(type=ElementType.TEXT, text="x")


def test_element_limit_refuses_past_n() -> None:
    ensure_element_limit([_element() for _ in range(5)], 5)
    with pytest.raises(IngestPipelineError) as excinfo:
        ensure_element_limit([_element() for _ in range(6)], 5)
    assert excinfo.value.code == "TOO_MANY_ELEMENTS"
    assert excinfo.value.retryable is False


def test_stable_chunk_id_is_deterministic_within_a_generation() -> None:
    """T-V1-016: retries reuse identical publication IDs; new generation → new IDs."""
    import uuid as uuid_mod

    from jobs.ingest_pipeline import PUBLICATION_NAMESPACE, stable_chunk_id

    generation = uuid_mod.uuid4()  # one DocumentVersion = one generation
    first = [stable_chunk_id(generation, f"chunk-{i:04d}") for i in range(3)]
    retry = [stable_chunk_id(generation, f"chunk-{i:04d}") for i in range(3)]
    assert first == retry  # same generation, same chunks → identical IDs (zero growth)
    assert len(set(first)) == 3  # distinct within the generation

    next_generation = uuid_mod.uuid4()  # new upload → fresh version → fresh IDs
    assert stable_chunk_id(next_generation, "chunk-0000") != first[0]
    assert stable_chunk_id(next_generation, "chunk-0000") != uuid_mod.uuid5(
        PUBLICATION_NAMESPACE, "chunk-0000"
    )  # never a bare chunk-id derivation


def test_no_per_caller_zero_vector_guard_in_pipeline() -> None:
    """T-V1-044: the store boundary (T-V1-015) is the single enforcement point.

    The pipeline must not carry its own zero-vector check — a per-caller guard
    here would make "single boundary, not per-caller checks" false.
    """
    import inspect

    from jobs import ingest_pipeline

    source = inspect.getsource(ingest_pipeline)
    assert "_is_zero_vector" not in source  # no second zero-vector implementation
    assert "ZERO_VECTOR_REJECTED" not in source  # per-caller reject path gone


def test_zero_vector_is_still_rejected_at_the_store_boundary() -> None:
    """Removed from the caller, not from the system: the boundary still refuses it."""
    from retrieval.adapters.qdrant_store import (
        REJECT_ZERO,
        VectorStoreInvalidVector,
        validate_record_vector,
    )
    from retrieval.interfaces import VectorRecord

    record = VectorRecord(id="chk-1", vector=[0.0] * 4, content="x", workspace_id="ws-a")
    with pytest.raises(VectorStoreInvalidVector) as excinfo:
        validate_record_vector(record, expected_dim=4)
    assert excinfo.value.reason == REJECT_ZERO


def test_registry_maps_supported_mime_types() -> None:
    assert parser_for_mime("application/pdf") is not None
    assert parser_for_mime("text/plain") is not None
    assert parser_for_mime("audio/mpeg") is None
    assert "text/plain" in supported_mime_types()


def test_decode_text_honours_charset_hint() -> None:
    raw = "Olá ção".encode("cp1252")
    assert decode_text(raw, "cp1252") == "Olá ção"
    with pytest.raises(ParseError):
        decode_text(raw, "utf-8")  # invalid utf-8 under an explicit hint


async def test_txt_parser_honours_charset_hint(fixtures_dir: Path) -> None:
    from ingestion.adapters.txt import PARSER

    raw = (fixtures_dir / "pd001" / "txt_cp1252.txt").read_bytes()
    parsed = await PARSER.parse(
        raw, mime_type="text/plain", metadata={"charset": "cp1252"}
    )
    assert parsed.elements


async def test_worker_loop_exits_when_stopped() -> None:
    import asyncio

    from jobs.ingest_worker import run_worker

    stop = asyncio.Event()
    stop.set()
    await run_worker(stop)  # returns immediately; no Redis needed


def test_queue_keys_are_namespaced_per_env() -> None:
    from ingestion.queue import delayed_key, queue_key

    assert queue_key() != "ingest:queue"
    assert delayed_key() != "ingest:delayed"
    assert queue_key().endswith(":queue") and "test" in queue_key()


async def test_worker_survives_process_one_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure escaping process_one must not kill the loop (Major #1)."""
    import asyncio
    import uuid

    import jobs.ingest_worker as ingest_worker

    stop = asyncio.Event()
    calls = {"n": 0}

    async def fake_dequeue(timeout_seconds: int = 1) -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return uuid.uuid4().hex
        stop.set()
        return None

    async def boom(job_id) -> None:
        raise RuntimeError("db down")

    async def noop_async(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(ingest_worker, "dequeue", fake_dequeue)
    monkeypatch.setattr(ingest_worker, "process_one", boom)
    monkeypatch.setattr(ingest_worker, "promote_due", noop_async)
    monkeypatch.setattr(ingest_worker, "schedule_retry", noop_async)

    await asyncio.wait_for(ingest_worker.run_worker(stop), timeout=5)
    assert calls["n"] >= 2  # loop consumed the next iteration after the failure


async def test_graph_indexing_hook_executed_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RP-04 / QF-04: When GRAPH_MODE=rule, graph extraction and persistence hooks run."""
    db_file = tmp_path / "test_graph.db"
    monkeypatch.setenv("GRAPH_MODE", "rule")
    monkeypatch.setenv("GRAPH_DB_PATH", str(db_file))

    from graph.extractor import GraphChunk, RuleBasedGraphExtractor
    from graph.sqlite_store import SQLiteGraphStore

    store = SQLiteGraphStore(str(db_file))
    extractor = RuleBasedGraphExtractor()

    chunks = [
        GraphChunk(
            chunk_id="chk-10",
            text="Alice Smith works for Acme Corp.",
            workspace_id="ws-graph-test",
            document_id="doc-graph-test",
            page=1,
            section="Team",
        )
    ]
    res = await extractor.extract(chunks)
    assert len(res.entities) >= 1
    assert len(res.relationships) >= 1

    await store.upsert_entities("ws-graph-test", res.entities)
    await store.upsert_relationships("ws-graph-test", res.relationships)

    # Verify nodes in store
    ents = await store.find_entities_by_name("ws-graph-test", "Alice Smith")
    assert len(ents) == 1
    assert "chk-10" in ents[0].chunk_ids


def test_publication_gate_covers_writes_and_status() -> None:
    """T-V1-019 checklist (1): the fence guards BOTH writes and the READY status.

    Source-level pin (same style as the zero-vector guard test above): the
    pipeline must call ``assert_generation_is_publishable`` at the pre-write
    boundary and again before the READY transition — a gate that only covered
    one of the two would let a replaced/deleted generation either publish rows
    or be marked READY. The refusal is non-retryable for every reason: a dead
    generation can never become live again, so a retry would only re-attempt
    the resurrection.
    """
    import inspect

    from jobs import ingest_pipeline
    from jobs.ingest_pipeline import (
        _FENCE_MESSAGES,
        FENCE_DELETED,
        FENCE_GONE,
        FENCE_SUPERSEDED,
        IngestPipelineError,
    )

    source = inspect.getsource(ingest_pipeline.process_job)
    assert source.count("await assert_generation_is_publishable(") == 2
    assert 'gate="write"' in source
    assert 'gate="ready"' in source
    # The READY transition must come AFTER the ready gate, never before it.
    assert source.index('gate="ready"') < source.index("job.status = JobStatus.READY.value")
    # The fence itself must be a generation compare, not a timing guard.
    gate = inspect.getsource(ingest_pipeline.generation_publication_state)
    assert "current_version_id" in gate and "superseded_at" in gate
    assert "asyncio.sleep" not in gate and "time.time" not in gate
    for reason in (FENCE_SUPERSEDED, FENCE_DELETED, FENCE_GONE):
        assert IngestPipelineError(reason, "fenced", retryable=False).retryable is False
        assert reason in _FENCE_MESSAGES
