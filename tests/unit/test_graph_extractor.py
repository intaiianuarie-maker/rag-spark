"""Unit tests for the V2 graph extractor seam (issues #1, #3, #4, #6).

Covers:
* issue #1 - workspace partitioning (reject mixed-workspace batches; identical
  names are scoped per workspace so nodes/edges/chunk provenance never leak).
* issue #3 - LLM adapter validates model output: names must be source-backed,
  relationship types must be known, and malformed JSON is rejected.
* issue #4 - the `graph` package imports cleanly after the adapters refactor.
* issue #6 - shared Q0 reason codes; failed chunks are not counted as
  provenance; ``chunk_batch_size`` must be positive.

No model weights, no API keys, no vendor SDKs: the LLM path uses a tiny
in-process :class:`LLMProvider` fake that returns canned JSON.
"""

from __future__ import annotations

import json

import graph
import pytest
from graph import (
    GraphChunk,
    RuleBasedGraphExtractor,
)
from graph.adapters import LLMBasedGraphExtractor
from graph.adapters.lightrag_extractor import _name_in_source
from graph.extractor import (
    ExtractionError,
    _reject_mixed_workspace,
)
from models.interfaces import GenerationRequest, GenerationResult, LLMProvider

# --------------------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------------------


class _FakeLLM(LLMProvider):
    """Deterministic LLMProvider returning a canned JSON payload."""

    provider_name = "fake-llm"

    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.calls = 0

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls += 1
        return GenerationResult(
            text=json.dumps(self._payload),
            model=request.model,
            finish_reason="stop",
        )

    def stream(self, request: GenerationRequest):  # pragma: no cover - unused
        async def _gen():
            yield json.dumps(self._payload)

        return _gen()


class _SequenceLLM(LLMProvider):
    """Returns a sequence of canned payloads, one per generate call."""

    provider_name = "fake-llm-seq"

    def __init__(self, outputs: list[object]) -> None:
        self._outputs = outputs
        self._i = 0
        self.calls = 0

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        out = self._outputs[min(self._i, len(self._outputs) - 1)]
        self._i += 1
        self.calls += 1
        return GenerationResult(
            text=json.dumps(out),
            model=request.model,
            finish_reason="stop",
        )

    def stream(self, request: GenerationRequest):  # pragma: no cover - unused
        async def _gen():
            yield json.dumps(self._outputs[0])

        return _gen()


def _chunk(ws: str, text: str, *, cid: str = "ch-1", did: str = "d-1") -> GraphChunk:
    return GraphChunk(chunk_id=cid, text=text, workspace_id=ws, document_id=did)


# --------------------------------------------------------------------------------------
# Issue #4 - import smoke
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_graph_package_imports_cleanly() -> None:
    """The package must import without side effects after the adapters refactor."""
    import importlib

    importlib.reload(graph)
    assert "LLMBasedGraphExtractor" in graph.__all__
    assert "SQLiteGraphStore" in graph.__all__
    assert "RuleBasedGraphExtractor" in graph.__all__


# --------------------------------------------------------------------------------------
# Issue #1 - rule-based workspace partitioning
# --------------------------------------------------------------------------------------


@pytest.mark.unit
async def test_rule_based_rejects_mixed_workspace_batch() -> None:
    extractor = RuleBasedGraphExtractor()
    res = await extractor.extract(
        [_chunk("ws-a", "Alice works at Acme."), _chunk("ws-b", "Bob works at Acme.")]
    )
    assert res.errors and res.errors[0].reason_code == "WORKSPACE_MISMATCH"
    assert res.entities == []
    assert res.relationships == []
    assert res.chunk_ids == []


@pytest.mark.unit
async def test_rule_based_extraction_provenance() -> None:
    text = (
        "Alice Smith works at Acme Corporation. "
        "Acme acquired Beta Corp. "
        "Acme is located in Paris."
    )
    res = await RuleBasedGraphExtractor().extract([_chunk("ws", text, cid="ch-1")])

    names = {(e.name, str(e.type)) for e in res.entities}
    assert ("Alice Smith", "PERSON") in names
    assert ("Acme Corporation", "ORGANIZATION") in names
    assert ("Paris", "LOCATION") in names

    by_id = {e.id: e.name for e in res.entities}
    rels = {
        (str(r.type), by_id[r.source_entity_id], by_id[r.target_entity_id])
        for r in res.relationships
    }
    assert ("WORKS_FOR", "Alice Smith", "Acme Corporation") in rels
    assert ("OWNS", "Acme Corporation", "Beta Corp") in rels
    assert ("LOCATED_IN", "Acme Corporation", "Paris") in rels

    assert res.chunk_ids == ["ch-1"]
    assert not res.errors
    assert {e.workspace_id for e in res.entities} == {"ws"}
    assert {r.workspace_id for r in res.relationships} == {"ws"}

# --------------------------------------------------------------------------------------
# Issue #1 + #6 - LLM adapter workspace partitioning + provenance
# --------------------------------------------------------------------------------------

_ALICE_ACME = {
    "entities": [
        {"name": "Alice", "type": "PERSON", "description": "a person"},
        {"name": "Acme", "type": "ORGANIZATION", "description": "an org"},
    ],
    "relationships": [
        {"source": "Alice", "type": "WORKS_FOR", "target": "Acme", "description": "works"}
    ],
}

_BOB_ACME = {
    "entities": [
        {"name": "Bob", "type": "PERSON", "description": "a person"},
        {"name": "Acme", "type": "ORGANIZATION", "description": "an org"},
    ],
    "relationships": [
        {"source": "Bob", "type": "WORKS_FOR", "target": "Acme", "description": "works"}
    ],
}


@pytest.mark.unit
async def test_llm_valid_extraction() -> None:
    extractor = LLMBasedGraphExtractor(
        _FakeLLM(_ALICE_ACME), "gpt-4o", resolve=False
    )
    res = await extractor.extract([_chunk("ws", "Alice works at Acme.")])
    assert {(e.name, str(e.type)) for e in res.entities} == {
        ("Alice", "PERSON"),
        ("Acme", "ORGANIZATION"),
    }
    assert len(res.relationships) == 1
    assert str(res.relationships[0].type) == "WORKS_FOR"
    assert res.chunk_ids == ["ch-1"]
    assert not res.errors


@pytest.mark.unit
async def test_llm_workspace_partitions_identical_names() -> None:
    """Identical 'Acme' in two workspaces must not cross-resolve or leak."""
    chunk_a = _chunk("ws-a", "Alice works at Acme.", cid="ch-a")
    chunk_b = _chunk("ws-b", "Bob works at Acme.", cid="ch-b")

    res_a = await LLMBasedGraphExtractor(
        _FakeLLM(_ALICE_ACME), "gpt-4o", resolve=False
    ).extract([chunk_a])
    res_b = await LLMBasedGraphExtractor(
        _FakeLLM(_BOB_ACME), "gpt-4o", resolve=False
    ).extract([chunk_b])

    assert {(e.name, str(e.type)) for e in res_a.entities} == {
        ("Alice", "PERSON"),
        ("Acme", "ORGANIZATION"),
    }
    assert len(res_a.relationships) == 1
    assert str(res_a.relationships[0].type) == "WORKS_FOR"
    assert res_a.chunk_ids == ["ch-a"]
    assert all(e.workspace_id == "ws-a" for e in res_a.entities)
    assert not any(e.name == "Bob" for e in res_a.entities)

    assert {(e.name, str(e.type)) for e in res_b.entities} == {
        ("Bob", "PERSON"),
        ("Acme", "ORGANIZATION"),
    }
    assert len(res_b.relationships) == 1
    assert res_b.chunk_ids == ["ch-b"]
    assert all(e.workspace_id == "ws-b" for e in res_b.entities)
    assert not any(e.name == "Alice" for e in res_b.entities)

    acme_a = next(e for e in res_a.entities if e.name == "Acme")
    acme_b = next(e for e in res_b.entities if e.name == "Acme")
    assert acme_a.id != acme_b.id

    fake_mixed = _FakeLLM(_ALICE_ACME)
    res_m = await LLMBasedGraphExtractor(fake_mixed, "gpt-4o").extract([chunk_a, chunk_b])
    assert res_m.errors and res_m.errors[0].reason_code == "WORKSPACE_MISMATCH"
    assert res_m.entities == [] and res_m.relationships == [] and res_m.chunk_ids == []
    assert fake_mixed.calls == 0


@pytest.mark.unit
async def test_llm_skips_failed_chunk_from_provenance() -> None:
    valid = {
        "entities": [{"name": "Alice", "type": "PERSON", "description": "p"}],
        "relationships": [],
    }
    hallucinated = {
        "entities": [{"name": "Project Phoenix", "type": "CONCEPT", "description": "p"}],
        "relationships": [],
    }
    extractor = LLMBasedGraphExtractor(
        _SequenceLLM([valid, hallucinated]), "gpt-4o", resolve=False
    )
    res = await extractor.extract(
        [_chunk("ws", "Alice works here.", cid="ch-valid"),
         _chunk("ws", "The meeting was productive.", cid="ch-bad")]
    )
    assert res.chunk_ids == ["ch-valid"]
    assert {(e.name, str(e.type)) for e in res.entities} == {("Alice", "PERSON")}
    assert res.errors and res.errors[0].reason_code == "EXTRACTION_UNVERIFIED"
    assert res.errors[0].workspace_id == "ws"
    assert res.errors[0].chunk_id == "ch-bad"


# --------------------------------------------------------------------------------------
# Issue #3 - LLM output validation
# --------------------------------------------------------------------------------------


@pytest.mark.unit
async def test_llm_rejects_hallucinated_entity() -> None:
    extractor = LLMBasedGraphExtractor(
        _FakeLLM({"entities": [{"name": "Project Phoenix", "type": "CONCEPT"}],
                  "relationships": []}),
        "gpt-4o", resolve=False,
    )
    res = await extractor.extract([_chunk("ws", "The meeting was productive.")])
    assert res.errors and res.errors[0].reason_code == "EXTRACTION_UNVERIFIED"
    assert res.errors[0].workspace_id == "ws"
    assert res.errors[0].chunk_id == "ch-1"
    assert res.entities == [] and res.chunk_ids == []


@pytest.mark.unit
async def test_llm_rejects_unknown_relationship_type() -> None:
    extractor = LLMBasedGraphExtractor(
        _FakeLLM({"entities": [], "relationships": [
            {"source": "Alice", "type": "MARRIED_TO", "target": "Acme"}
        ]}),
        "gpt-4o", resolve=False,
    )
    res = await extractor.extract([_chunk("ws", "Alice works at Acme.")])
    assert res.errors and res.errors[0].reason_code == "LLM_SCHEMA_INVALID"
    assert res.entities == [] and res.chunk_ids == []


@pytest.mark.unit
async def test_llm_rejects_malformed_json() -> None:
    extractor = LLMBasedGraphExtractor(_FakeLLM("not json {"), "gpt-4o", resolve=False)
    res = await extractor.extract([_chunk("ws", "Alice works at Acme.")])
    assert res.errors and res.errors[0].reason_code == "LLM_PARSE_FAILED"
    assert res.errors[0].workspace_id == "ws"
    assert res.errors[0].chunk_id == "ch-1"
    assert res.entities == [] and res.chunk_ids == []


@pytest.mark.unit
async def test_llm_accepts_fenced_json() -> None:
    payload = json.dumps(_ALICE_ACME)
    extractor = LLMBasedGraphExtractor(
        _FakeLLM(f"```json\n{payload}\n```"), "gpt-4o", resolve=False
    )
    res = await extractor.extract([_chunk("ws", "Alice works at Acme.")])
    assert len(res.entities) == 2 and len(res.relationships) == 1
    assert not res.errors

# --------------------------------------------------------------------------------------
# Issue #6 - reason codes + guard rails
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_reason_code_is_q0_style() -> None:
    err = ExtractionError(
        "EXTRACTION_UNVERIFIED", "msg", workspace_id="ws", chunk_id="c"
    )
    assert isinstance(err, Exception)
    assert err.reason_code == "EXTRACTION_UNVERIFIED"
    assert err.workspace_id == "ws"
    assert err.chunk_id == "c"
    assert err.message == "msg"


@pytest.mark.unit
async def test_llm_chunk_batch_size_must_be_positive() -> None:
    with pytest.raises(ValueError, match="chunk_batch_size"):
        LLMBasedGraphExtractor(_FakeLLM({}), "gpt-4o", chunk_batch_size=0)
    with pytest.raises(ValueError, match="chunk_batch_size"):
        LLMBasedGraphExtractor(_FakeLLM({}), "gpt-4o", chunk_batch_size=-1)


@pytest.mark.unit
def test_reject_mixed_workspace_direct() -> None:
    err = _reject_mixed_workspace([_chunk("ws-a", "x"), _chunk("ws-b", "y")])
    assert err is not None and err.reason_code == "WORKSPACE_MISMATCH"
    assert _reject_mixed_workspace([_chunk("ws", "x"), _chunk("ws", "y")]) is None


@pytest.mark.unit
def test_name_in_source_rejects_hallucination() -> None:
    assert _name_in_source("Acme Corp", "Alice works at Acme Corp.") is True
    assert _name_in_source("Acme", "Alice works at Acme.") is True
    assert _name_in_source("Project Phoenix", "The meeting was productive.") is False


def test_name_in_source_rejects_invented_names_and_substrings() -> None:
    """Cover the P2 finding: invented full names and substring false-positives.

    - Invented surname: "Alice InventedSurname" must be rejected even though
      "Alice" appears in the source.
    - Substring match: "Ann" inside "Annual" must be rejected (word-boundary
      requirement).
    - Recombined name: "Alice Smith" from "Alice Jones met Bob Smith" must be
      rejected (tokens present but not adjacent in order).
    - Reordered: "Smith Alice" where text has "Alice Smith" must be rejected.
    """
    assert _name_in_source("Alice InventedSurname", "Alice works here.") is False
    assert _name_in_source("Ann", "Annual report.") is False
    assert _name_in_source("Alice Smith", "Alice Jones met Bob Smith.") is False
    assert _name_in_source("Smith Alice", "Alice Smith is here.") is False
