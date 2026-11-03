"""``CHUNKING_BACKEND`` selection in the ingest pipeline (plan G6).

``docling_hybrid`` must work end to end; every fallback is loud (a
``chunking_backend_fallback`` warning) and the backend that *actually* ran is
what the stage log reports.
"""

from __future__ import annotations

import inspect
import logging
import os

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytest.importorskip("docling_core.transforms.chunker.hybrid_chunker")

from ingestion.adapters import docling_chunker
from ingestion.adapters.llamaindex_chunker import chunk_document as llamaindex_chunk_document
from ingestion.interfaces import DocumentElement, ElementType, ParsedDocument
from jobs import ingest_pipeline

pytestmark = pytest.mark.unit

LLAMA = ingest_pipeline.llamaindex_backend_name()


def _parsed() -> ParsedDocument:
    return ParsedDocument(
        elements=[
            DocumentElement(type=ElementType.HEADING, text="Intro", page=1),
            DocumentElement(
                type=ElementType.TEXT, text="Plain body text.", page=1, section="Intro"
            ),
            DocumentElement(
                type=ElementType.TEXT,
                text="[Model-generated page description, page 2] A chart.",
                page=2,
                metadata={"extraction": "vision"},
            ),
        ],
        language="en",
    )


def _fallbacks(caplog):
    return [r for r in caplog.records if getattr(r, "event", None) == "chunking_backend_fallback"]


def test_docling_hybrid_runs_end_to_end(monkeypatch, caplog):
    monkeypatch.setattr(ingest_pipeline, "CHUNKING_BACKEND", "docling_hybrid")
    with caplog.at_level(logging.WARNING):
        result, used = ingest_pipeline._chunk_with_backend(_parsed(), filename="a.pdf")
    assert used == docling_chunker.BACKEND_NAME == ingest_pipeline.resolved_chunking_backend()
    assert [c.metadata.get("extraction") for c in result.chunks] == [None, "vision"]
    assert all(c.metadata["chunking_backend"] == "docling_hybrid" for c in result.chunks)
    assert _fallbacks(caplog) == []
    assert ingest_pipeline._chunk(_parsed(), filename="a.pdf").chunks == result.chunks


def test_llamaindex_path_is_unchanged(monkeypatch, caplog):
    monkeypatch.setattr(ingest_pipeline, "CHUNKING_BACKEND", "llamaindex")
    with caplog.at_level(logging.WARNING):
        result, used = ingest_pipeline._chunk_with_backend(_parsed(), filename="a.pdf")
    assert used == LLAMA
    assert result == llamaindex_chunk_document(_parsed(), filename="a.pdf")
    assert _fallbacks(caplog) == []


def test_builtin_path_reports_builtin(monkeypatch):
    monkeypatch.setattr(ingest_pipeline, "CHUNKING_BACKEND", "builtin")
    _, used = ingest_pipeline._chunk_with_backend(_parsed(), filename=None)
    assert used == "builtin-whitespace" == ingest_pipeline.resolved_chunking_backend()


def test_missing_docling_core_falls_back_loudly(monkeypatch, caplog):
    monkeypatch.setattr(ingest_pipeline, "CHUNKING_BACKEND", "docling_hybrid")
    monkeypatch.setitem(__import__("sys").modules, "ingestion.adapters.docling_chunker", None)
    with caplog.at_level(logging.WARNING):
        result, used = ingest_pipeline._chunk_with_backend(_parsed(), filename="a.pdf")
        assert ingest_pipeline.resolved_chunking_backend() == LLAMA
    assert used == LLAMA
    assert result.chunks  # still ingests
    (warning,) = _fallbacks(caplog)
    assert warning.requested == "docling_hybrid" and "ModuleNotFoundError" in warning.reason


def test_chunker_exception_falls_back_loudly(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("hybrid exploded")

    monkeypatch.setattr(ingest_pipeline, "CHUNKING_BACKEND", "docling_hybrid")
    monkeypatch.setattr(docling_chunker, "chunk_document_hybrid", boom)
    with caplog.at_level(logging.WARNING):
        result, used = ingest_pipeline._chunk_with_backend(_parsed(), filename="a.pdf")
    assert used == LLAMA and result.chunks
    (warning,) = _fallbacks(caplog)
    assert "RuntimeError: hybrid exploded" in warning.reason


def test_unknown_backend_value_warns_and_uses_llamaindex(monkeypatch, caplog):
    monkeypatch.setattr(ingest_pipeline, "CHUNKING_BACKEND", "dockling")
    with caplog.at_level(logging.WARNING):
        result, used = ingest_pipeline._chunk_with_backend(_parsed(), filename="a.pdf")
    assert used == LLAMA and result.chunks
    (warning,) = _fallbacks(caplog)
    assert warning.requested == "dockling" and "unknown" in warning.reason


def test_stage_log_records_the_backend_that_actually_ran():
    source = inspect.getsource(ingest_pipeline.process_job)
    assert "result, backend_used = _chunk_with_backend(" in source
    assert '"resolved": backend_used' in source
    assert '"resolved": resolved_chunking_backend()' not in source
