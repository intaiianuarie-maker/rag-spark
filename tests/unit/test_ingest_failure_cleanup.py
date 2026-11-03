"""A failed ingest must not stay searchable; job error_message must not leak internals."""

from __future__ import annotations

import asyncio
import types
import uuid
from datetime import UTC, datetime

import pytest
from ingestion.persistence import JobStage
from jobs import ingest_pipeline as ip

pytestmark = pytest.mark.unit

DOC, VER = uuid.uuid4(), uuid.uuid4()


T0 = datetime(2026, 1, 1, tzinfo=UTC)
RUNNING = ("running", 1, T0)  # fresh re-read row: (status, attempts, started_at)


class _Result:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class _Session:
    def __init__(self, stage, fresh):
        self.job = types.SimpleNamespace(
            document_id=DOC, document_version_id=VER, stage=stage, attempts=1, started_at=T0
        )
        self.fresh = fresh  # an Exception instance = the re-read fails
        self.events: list[str] = []

    async def get(self, _model, _id):
        return self.job

    async def rollback(self):
        self.events.append("rollback")

    async def execute(self, _stmt):
        if isinstance(self.fresh, Exception):
            raise self.fresh
        return _Result(self.fresh)


def _run(stage, exc, monkeypatch, fresh=RUNNING, live=None):
    session = _Session(stage, fresh)

    async def unpublish(_s, *, document_id, version_id):
        session.events.append(("unpublish", document_id, version_id))

    async def state(_s, *, document_id, version_id):
        return live  # None = generation still live

    monkeypatch.setattr(ip, "_unpublish_generation", unpublish)
    monkeypatch.setattr(ip, "generation_publication_state", state)

    async def body(_s, _jid):
        if exc is None:
            return "ok"
        raise exc

    wrapped = ip._unpublish_on_failure(body)
    try:
        out = asyncio.run(wrapped(session, uuid.uuid4()))
    except Exception as e:
        out = e
    return session.events, out


def test_process_job_is_wrapped():
    assert hasattr(ip.process_job, "__wrapped__")


@pytest.mark.parametrize(
    "stage", [JobStage.CHUNKING.value, JobStage.INDEXING.value, JobStage.EMBEDDING.value]
)
def test_embed_failure_after_indexing_unpublishes_after_rollback(stage, monkeypatch):
    err = ip.IngestPipelineError("EMBED_UNAVAILABLE", "boom", retryable=True)
    events, out = _run(stage, err, monkeypatch)
    assert out is err  # original error still reaches the worker (retry/park intact)
    assert events == ["rollback", ("unpublish", DOC, VER)]


def test_non_write_stage_failure_touches_nothing(monkeypatch):
    err = ip.IngestPipelineError("PARSE_FAILED", "x")
    events, _ = _run(JobStage.PARSING.value, err, monkeypatch)
    assert events == []


def test_fence_refusal_is_not_unpublished_twice(monkeypatch):
    err = ip.IngestPipelineError(ip.FENCE_DELETED, "fenced", retryable=False)
    events, _ = _run(JobStage.EMBEDDING.value, err, monkeypatch)
    assert events == []


def test_success_touches_nothing(monkeypatch):
    events, out = _run(JobStage.EMBEDDING.value, None, monkeypatch)
    assert out == "ok" and events == []


def test_unexpected_exception_also_unpublishes(monkeypatch):
    events, out = _run(JobStage.INDEXING.value, RuntimeError("db"), monkeypatch)
    assert isinstance(out, RuntimeError)
    assert events == ["rollback", ("unpublish", DOC, VER)]


@pytest.mark.parametrize(
    ("fresh", "live", "reason"),
    [
        (("ready", 2, T0), None, "job_status_ready"),  # B finished READY
        (("failed", 1, T0), None, "job_status_failed"),
        (("queued", 1, T0), None, "job_status_queued"),
        (("running", 2, T0), None, "attempt_changed"),  # redelivered copy B
        (("running", 1, datetime(2026, 1, 2, tzinfo=UTC)), None, "lease_owner_changed"),
        (None, None, "job_missing"),
        (("running", 1, T0), "STALE_GENERATION", "generation_not_live"),
        (RuntimeError("db down"), None, "recheck_failed"),  # fail safe: no deletion
    ],
)
def test_cleanup_skipped_when_run_no_longer_owns_the_job(fresh, live, reason, monkeypatch, caplog):
    err = ip.IngestPipelineError("STORE_WRITE_UNVERIFIED", "count mismatch", retryable=True)
    with caplog.at_level("WARNING"):
        events, out = _run(JobStage.INDEXING.value, err, monkeypatch, fresh=fresh, live=live)
    assert out is err  # original error still re-raised
    assert not any(isinstance(e, tuple) for e in events)  # nothing unpublished
    skipped = [r for r in caplog.records if r.getMessage() == "ingest_cleanup_skipped"]
    assert len(skipped) == 1 and skipped[0].reason.startswith(reason)


def test_cleanup_runs_when_still_running_and_owned(monkeypatch):
    err = ip.IngestPipelineError("EMBED_UNAVAILABLE", "boom", retryable=True)
    events, out = _run(JobStage.EMBEDDING.value, err, monkeypatch, fresh=RUNNING)
    assert out is err
    assert events == ["rollback", ("unpublish", DOC, VER)]


@pytest.mark.parametrize("code", sorted(ip._DETAIL_BEARING_CODES))
def test_error_message_is_short_coded_and_detail_is_kept(code):
    leak = "DoclingParser raised KeyError: 'cells' trailer {'/Root': 1} password=hunter2"
    err = ip.IngestPipelineError(code, leak)
    assert err.code == code
    assert "Docling" not in err.message and "trailer" in err.detail
    assert err.message and len(err.message) < 120
    assert err.detail == leak


def test_safe_messages_pass_through_unchanged():
    err = ip.IngestPipelineError("EMPTY_DOCUMENT", "empty document", retryable=False)
    assert err.message == "empty document"
