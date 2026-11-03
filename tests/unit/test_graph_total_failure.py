"""Graph total failure must not look like success (LLM_SCHEMA_INVALID on every chunk)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types

import pytest

sys.path.insert(0, "packages")
sys.path.insert(0, "apps/worker")
sys.path.insert(0, "apps/api")

from graph.adapters.lightrag_extractor import LLMBasedGraphExtractor
from graph.extractor import GraphChunk
from jobs import graph_extraction as ge
from models.interfaces import GenerationResult

pytestmark = pytest.mark.unit

_WS = "ws"


class _LLM:
    provider_name = "fake"

    def __init__(self, replies):
        self._replies = list(replies)

    async def generate(self, request):
        r = self._replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return GenerationResult(
            text=json.dumps(r), model=request.model, prompt_tokens=1, completion_tokens=1
        )


def _chunk(cid, text):
    return GraphChunk(chunk_id=cid, text=text, workspace_id=_WS, document_id="d")


_BAD_TYPE = {"entities": [{"name": "Alice", "type": "NOPE"}], "relationships": []}
_GOOD = {"entities": [{"name": "Alice", "type": "PERSON", "description": "p"}], "relationships": []}


def test_extractor_records_failed_chunk_ids_for_schema_and_provider_errors():
    ex = LLMBasedGraphExtractor(_LLM([_BAD_TYPE, RuntimeError("boom"), _GOOD]), "m", resolve=False)
    res = asyncio.run(
        ex.extract([_chunk("a", "Alice"), _chunk("b", "Alice"), _chunk("c", "Alice")])
    )
    assert res.failed_chunk_ids == ["a", "b"]
    assert res.chunk_ids == ["c"]
    assert [e.reason_code for e in res.errors] == ["LLM_SCHEMA_INVALID", "LLM_EXTRACTION_FAILED"]


class _Row:
    def __init__(self, cid):
        self.id, self.text, self.page, self.section, self.language = cid, "Alice", 1, None, "en"


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, _stmt):
        rows = [_Row(__import__("uuid").uuid4()) for _ in range(2)]
        return types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: rows))


class _Store:
    def __init__(self):
        self.upserts = 0

    async def is_document_tombstoned(self, *a):
        return False

    async def upsert_extraction(self, *a, **k):
        self.upserts += 1

    def close(self):
        pass


def _wire(monkeypatch, replies):
    import uuid

    async def payload(_jid):
        return {"workspace_id": _WS, "document_id": "d", "version_id": str(uuid.uuid4())}

    store = _Store()
    monkeypatch.setattr(ge, "get_payload", payload)
    monkeypatch.setattr(ge, "get_session_factory", lambda: lambda: _Session())
    monkeypatch.setattr(ge, "graph_mode", lambda: "rule")
    monkeypatch.setattr(
        ge,
        "create_graph_extractor",
        lambda: LLMBasedGraphExtractor(_LLM(replies), "m", resolve=False),
    )
    monkeypatch.setattr(ge, "create_graph_store", lambda: store)
    return store


def test_total_failure_raises_instead_of_success(monkeypatch):
    store = _wire(monkeypatch, [_BAD_TYPE, _BAD_TYPE])
    with pytest.raises(RuntimeError, match="all 2 chunks"):
        asyncio.run(ge.process_graph_job("j"))
    assert store.upserts == 0


def test_partial_failure_persists_and_is_reported(monkeypatch, caplog):
    store = _wire(monkeypatch, [_BAD_TYPE, _GOOD])
    with caplog.at_level(logging.WARNING, logger=ge.logger.name):
        out = asyncio.run(ge.process_graph_job("j"))
    assert out["partial"] is True and len(out["failed_chunks"]) == 1
    assert store.upserts == 1
    assert any(r.getMessage() == "graph_extraction_partial" for r in caplog.records)
