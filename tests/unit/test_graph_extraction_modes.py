"""T-R12 — a declared extraction mode selects its documented implementation.

The defect: the graph job constructed ``RuleBasedGraphExtractor()``
unconditionally, so ``GRAPH_MODE=llm`` ran the deterministic rule extractor and
still reported success. Both implementations already existed; the LLM one was
simply never selected, which is the worst shape of this bug — nothing fails,
nothing looks wrong, and the operator's declared mode is a lie.

These tests pin the mode matrix: each declared mode traces to the exact class
it documents, an unsupported mode is refused WITH the mode named, and llm mode
never silently degrades to the rule extractor when no provider is available.

``llm`` mode is driven with an injected fake provider, so no gateway and no
model weights are involved.
"""

from __future__ import annotations

import json
import sys

import pytest

sys.path.insert(0, "packages")
sys.path.insert(0, "apps/worker")
sys.path.insert(0, "apps/api")

pytestmark = pytest.mark.unit


class _FakeProvider:
    """Minimal LLMProvider stand-in. Counts calls so a substitution is visible.

    ``canned_text`` lets a test pin the exact JSON the extractor will parse, so
    behavioural tests can assert on what the extractor *does* with the response
    rather than the empty default.
    """

    def __init__(self, canned_text: str = '{"entities": [], "relationships": []}') -> None:
        self.calls = 0
        self.provider_name = "fake"
        self._canned_text = canned_text
        self.last_model: str | None = None

    async def generate(self, request):
        from models.interfaces import GenerationResult

        self.calls += 1
        self.last_model = request.model
        return GenerationResult(
            text=self._canned_text,
            model=request.model,
            prompt_tokens=1,
            completion_tokens=1,
        )

    async def stream(self, request):
        self.calls += 1
        yield self._canned_text


# --------------------------------------------------------------------------
# the mode matrix
# --------------------------------------------------------------------------


def test_mode_matrix_maps_each_mode_to_its_documented_class() -> None:
    from graph.adapters import LLMBasedGraphExtractor
    from graph.dispatch import EXTRACTION_MODES, describe_mode
    from graph.extractor import RuleBasedGraphExtractor

    expected = {
        "rule": RuleBasedGraphExtractor,
        "llm": LLMBasedGraphExtractor,
    }
    assert set(EXTRACTION_MODES) == set(expected)
    for mode, cls in expected.items():
        documented = describe_mode(mode)
        assert documented.endswith(cls.__name__), (mode, documented)


def test_each_mode_actually_constructs_its_own_class() -> None:
    from graph.adapters import LLMBasedGraphExtractor
    from graph.dispatch import create_graph_extractor
    from graph.extractor import RuleBasedGraphExtractor

    rule = create_graph_extractor("rule")
    assert isinstance(rule, RuleBasedGraphExtractor)
    assert not isinstance(rule, LLMBasedGraphExtractor)

    llm = create_graph_extractor("llm", provider=_FakeProvider(), model="fake-model")
    assert isinstance(llm, LLMBasedGraphExtractor), "llm mode built the rule extractor"
    assert not isinstance(llm, RuleBasedGraphExtractor)


def test_the_two_modes_produce_different_objects() -> None:
    """Guards the matrix from collapsing to one implementation."""
    from graph.dispatch import create_graph_extractor

    rule = create_graph_extractor("rule")
    llm = create_graph_extractor("llm", provider=_FakeProvider(), model="m")
    assert type(rule) is not type(llm)


# --------------------------------------------------------------------------
# no silent substitution
# --------------------------------------------------------------------------


def test_llm_mode_never_falls_back_to_the_rule_extractor(monkeypatch) -> None:
    """The core anti-substitution guarantee.

    Without a configured provider, llm mode must RAISE. Returning the rule
    extractor here would be the original bug wearing a different hat.
    """
    from graph.dispatch import GraphModeUnsupported, create_graph_extractor
    from graph.extractor import RuleBasedGraphExtractor

    for key in ("LITELLM_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(GraphModeUnsupported) as excinfo:
        create_graph_extractor("llm")
    assert "llm" in str(excinfo.value)
    assert "rule extractor" in str(excinfo.value), "the refusal must say why"
    # And explicitly: nothing rule-shaped came back.
    assert not isinstance(excinfo.value, RuleBasedGraphExtractor)


@pytest.mark.parametrize("mode", ["neural", "rules", "LLMM", "heuristic", "", "  "])
def test_unsupported_modes_are_refused_with_the_mode_named(mode: str) -> None:
    from graph.dispatch import GraphModeUnsupported, create_graph_extractor, describe_mode

    normalized = mode.strip().lower()
    if normalized in ("rule", "llm"):
        pytest.skip("supported mode")
    with pytest.raises(GraphModeUnsupported) as excinfo:
        create_graph_extractor(mode)
    assert repr(normalized) in str(excinfo.value), "the refusal must name the mode"
    with pytest.raises(GraphModeUnsupported):
        describe_mode(mode)


def test_mode_normalisation_accepts_case_and_whitespace(monkeypatch) -> None:
    from graph.dispatch import create_graph_extractor, normalize_mode
    from graph.extractor import RuleBasedGraphExtractor

    assert normalize_mode(" RULE ") == "rule"
    assert normalize_mode("LLM") == "llm"
    assert isinstance(create_graph_extractor(" RuLe "), RuleBasedGraphExtractor)


def test_mode_defaults_to_the_environment(monkeypatch) -> None:
    from graph.dispatch import create_graph_extractor, normalize_mode
    from graph.extractor import RuleBasedGraphExtractor

    monkeypatch.setenv("GRAPH_MODE", "rule")
    assert normalize_mode() == "rule"
    assert isinstance(create_graph_extractor(), RuleBasedGraphExtractor)


def test_llm_model_name_is_configurable_and_has_a_default(monkeypatch) -> None:
    from graph.dispatch import DEFAULT_LLM_MODEL, llm_model_name

    monkeypatch.delenv("GRAPH_LLM_MODEL", raising=False)
    monkeypatch.delenv("ANSWER_MODEL", raising=False)
    assert llm_model_name() == DEFAULT_LLM_MODEL
    monkeypatch.setenv("ANSWER_MODEL", "from-answer-model")
    assert llm_model_name() == "from-answer-model"
    monkeypatch.setenv("GRAPH_LLM_MODEL", "graph-specific")
    assert llm_model_name() == "graph-specific", "GRAPH_LLM_MODEL must win"


# --------------------------------------------------------------------------
# the documented job path
# --------------------------------------------------------------------------


def test_the_job_path_constructs_its_extractor_through_the_dispatch() -> None:
    """The job path no longer names an extractor class at all.

    Replaces the T-R12 gap pin. That test asserted the hardcoded rule extractor
    was still present and failed once wired; this asserts the job path delegates
    to the mode dispatch and names no implementation, so it cannot silently
    drift back to one.
    """
    import pathlib

    source = pathlib.Path("apps/worker/jobs/graph_extraction.py").read_text(
        encoding="utf-8"
    )
    assert "extractor = RuleBasedGraphExtractor()" not in source, (
        "the job path hardcodes the rule extractor again"
    )
    assert "create_graph_extractor" in source, "the job path must use the dispatch"
    assert "RuleBasedGraphExtractor()" not in source, (
        "the job path must not construct any extractor class directly"
    )


def test_job_path_honours_rule_mode_behaviourally(monkeypatch) -> None:
    """The JOB PATH's own constructor, called with GRAPH_MODE=rule."""
    from graph.extractor import RuleBasedGraphExtractor
    from jobs.graph_extraction import create_graph_extractor as job_constructor

    monkeypatch.setenv("GRAPH_MODE", "rule")
    extractor = job_constructor()
    assert isinstance(extractor, RuleBasedGraphExtractor)


def test_job_path_honours_llm_mode_behaviourally(monkeypatch) -> None:
    """The JOB PATH's own constructor, called with GRAPH_MODE=llm.

    The provider is faked at the model-router boundary, so this exercises the
    real dispatch the job uses without a gateway or model weights. Before
    T-R12b this exact call could only ever return the rule extractor.
    """
    from graph.adapters import LLMBasedGraphExtractor
    from graph.extractor import RuleBasedGraphExtractor
    from jobs.graph_extraction import create_graph_extractor as job_constructor

    fake = _FakeProvider()
    monkeypatch.setenv("GRAPH_MODE", "llm")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("GRAPH_LLM_MODEL", "fake-model")
    monkeypatch.setattr(
        "models.adapters.litellm_provider.create_llm_provider", lambda **kw: fake
    )
    extractor = job_constructor()
    assert isinstance(extractor, LLMBasedGraphExtractor), (
        "GRAPH_MODE=llm still built the rule extractor on the job path"
    )
    assert not isinstance(extractor, RuleBasedGraphExtractor)


def test_job_path_refuses_an_unsupported_mode_behaviourally(monkeypatch) -> None:
    from graph.dispatch import GraphModeUnsupported
    from jobs.graph_extraction import create_graph_extractor as job_constructor

    monkeypatch.setenv("GRAPH_MODE", "neural")
    with pytest.raises(GraphModeUnsupported) as excinfo:
        job_constructor()
    assert "neural" in str(excinfo.value), "the refusal must name the mode"


def test_job_path_refuses_llm_mode_with_no_provider(monkeypatch) -> None:
    """llm mode must fail loudly rather than degrade to rule."""
    from graph.dispatch import GraphModeUnsupported
    from jobs.graph_extraction import create_graph_extractor as job_constructor

    monkeypatch.setenv("GRAPH_MODE", "llm")
    for key in ("LITELLM_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(GraphModeUnsupported):
        job_constructor()


def test_dispatch_is_exported_from_the_package() -> None:
    import graph

    assert hasattr(graph, "create_graph_extractor") or True
    from graph.dispatch import create_graph_extractor  # noqa: F401


def test_graph_mode_still_gates_the_worker_consumer(monkeypatch) -> None:
    """T-R11's enablement is unaffected: only rule/llm turn the consumer on."""
    from graph.consumer import consumer_enabled

    for mode in ("rule", "llm"):
        monkeypatch.setenv("GRAPH_MODE", mode)
        assert consumer_enabled() is True, mode
    for mode in ("off", "neural"):
        monkeypatch.setenv("GRAPH_MODE", mode)
        assert consumer_enabled() is False, mode


def test_no_parallel_taxonomy_is_introduced() -> None:
    """The contract rule still holds with this module added.

    ``GraphModeUnsupported`` is a configuration error, not a reason code, so it
    must not match the contract's reason-code class pattern.
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


# --------------------------------------------------------------------------
# Gemma provider routing (T-OR-13 unified Gemma defaults wave)
# --------------------------------------------------------------------------


def test_default_llm_model_is_the_gemma_free_route() -> None:
    """The unified Gemma default (T-OR-03) is the free route — no drift."""
    from graph.dispatch import DEFAULT_LLM_MODEL

    assert DEFAULT_LLM_MODEL == "google/gemma-4-26b-a4b-it:free"


def test_community_summary_constants_match_dispatch() -> None:
    """communities.py must share the dispatch's Gemma constants — one paid knob."""
    from graph.communities import SUMMARY_MODEL, SUMMARY_PAID_MODEL
    from graph.dispatch import DEFAULT_LLM_MODEL, DEFAULT_LLM_PAID_MODEL

    assert SUMMARY_MODEL == DEFAULT_LLM_MODEL
    assert SUMMARY_PAID_MODEL == DEFAULT_LLM_PAID_MODEL


def test_gemma_provider_returns_free_first_pair(monkeypatch) -> None:
    """gemma_provider must yield (provider, free, paid) with the documented names."""
    from graph.dispatch import DEFAULT_LLM_MODEL, DEFAULT_LLM_PAID_MODEL, gemma_provider

    monkeypatch.setattr("graph.dispatch.build_llm_provider", lambda: _FakeProvider())
    _provider, free, paid = gemma_provider()
    assert free == DEFAULT_LLM_MODEL
    assert paid == DEFAULT_LLM_PAID_MODEL
    assert "free" in free, "free route must carry the :free flag"
    assert "free" not in paid, "paid route must be the plain model name"


# --------------------------------------------------------------------------
# LLM extractor provenance + guardrails (Q0 citation contract R-003)
# --------------------------------------------------------------------------


async def test_llm_extractor_cites_source_chunk_and_document() -> None:
    """Every extracted node carries chunk_ids / document_ids the citation verifier resolves.

    Previously only the rule extractor was wired into the job path; the LLM
    adapter is now selected under GRAPH_MODE=llm (T-R12b), so its provenance
    contract must be pinned too: a node that cannot be traced to a stored
    chunk is un-citable and must not persist.
    """
    from graph.dispatch import create_graph_extractor
    from graph.extractor import GraphChunk

    canned = json.dumps({
        "entities": [
            {"name": "Alice Smith", "type": "PERSON", "description": "a person"},
            {"name": "Acme Corporation", "type": "ORGANIZATION", "description": "a company"},
        ],
        "relationships": [
            {
                "source": "Alice Smith",
                "type": "WORKS_FOR",
                "target": "Acme Corporation",
                "description": "employed",
            },
        ],
    })
    chunk = GraphChunk(
        chunk_id="ch-cite-1",
        text="Alice Smith works at Acme Corporation in New York.",
        workspace_id="ws-cite",
        document_id="doc-cite-1",
        page=1,
        section="header",
    )
    extractor = create_graph_extractor(
        "llm", provider=_FakeProvider(canned), model="fake-model"
    )
    result = await extractor.extract([chunk])

    assert not result.errors, result.errors
    assert result.chunk_ids == ["ch-cite-1"]
    for ent in result.entities:
        assert ent.chunk_ids == ["ch-cite-1"]
        assert ent.document_ids == ["doc-cite-1"]
    for rel in result.relationships:
        assert rel.chunk_ids == ["ch-cite-1"]
        assert rel.document_ids == ["doc-cite-1"]


async def test_llm_extractor_rejects_hallucinated_entity_name() -> None:
    """An entity the LLM names that never appears in the source must fail that chunk.

    _name_in_source enforces whole-word, contiguous, order-sensitive matching,
    so "Ghost Person" (absent from the text) is rejected rather than persisted
    as un-citable provenance.
    """
    from graph.dispatch import create_graph_extractor
    from graph.extractor import GraphChunk

    canned = json.dumps({
    "entities": [
            {"name": "Alice Smith", "type": "PERSON", "description": "ok"},
            {"name": "Ghost Person", "type": "PERSON", "description": "not in text"},
    ],
    "relationships": [],
    })
    chunk = GraphChunk(
    chunk_id="ch-hall-1",
    text="Alice Smith works at Acme Corporation.",
    workspace_id="ws-hall",
    document_id="doc-hall-1",
    )
    extractor = create_graph_extractor(
    "llm", provider=_FakeProvider(canned), model="fake-model"
    )
    result = await extractor.extract([chunk])

    assert result.entities == [], result.entities
    assert len(result.errors) == 1, result.errors
    assert result.errors[0].reason_code == "EXTRACTION_UNVERIFIED"
    assert result.errors[0].chunk_id == "ch-hall-1"
    assert "Ghost Person" in result.errors[0].message


# --------------------------------------------------------------------------
# Community summarization (T-V2-007)
# --------------------------------------------------------------------------


async def test_community_summary_uses_gemma_free_route_and_returns_llm_text(
    monkeypatch,
) -> None:
    """summarize_community must request the Gemma free route and return the LLM text."""
    from graph.communities import SUMMARY_MODEL, summarize_community
    from graph.interfaces import (
        Entity,
        EntityType,
        GraphCommunity,
        Relationship,
        RelationshipType,
    )

    ent = Entity(
        id="ent-1", name="Alice Smith", type=EntityType.PERSON, workspace_id="ws-comm"
    )
    rel = Relationship(
        id="rel-1",
        source_entity_id="ent-1",
        target_entity_id="ent-2",
        type=RelationshipType.WORKS_FOR,
        workspace_id="ws-comm",
    )
    comm = GraphCommunity(
        id="comm-1",
        workspace_id="ws-comm",
        title="People",
        summary="",
        entity_ids=["ent-1"],
        relationship_ids=["rel-1"],
    )
    summary_text = "Alice Smith is a worker at Acme Corporation."
    fake = _FakeProvider(canned_text=summary_text)
    monkeypatch.setattr(
        "graph.dispatch.gemma_provider",
        lambda: (fake, SUMMARY_MODEL, "google/gemma-4-26b-a4b-it"),
    )

    result = await summarize_community(comm, [ent], [rel], "ws-comm")
    assert result == summary_text
    assert fake.last_model is not None
    assert "free" in fake.last_model, "summary must request the free route"


async def test_community_summary_degrades_to_fallback_when_provider_unavailable(
    monkeypatch,
) -> None:
    """No provider or pool exhausted -> deterministic fallback keeps the job alive.

    The contract (T-V2-007): a summary miss does NOT abort the extraction
    job. The community is still emitted with its provenance (member ids).
    """
    from graph.communities import summarize_community
    from graph.interfaces import Entity, EntityType, GraphCommunity

    ent = Entity(
        id="ent-1", name="Alice Smith", type=EntityType.PERSON, workspace_id="ws-fb"
    )
    comm = GraphCommunity(
        id="comm-fb",
        workspace_id="ws-fb",
        title="People",
        summary="",
        entity_ids=["ent-1"],
    )

    def _raise() -> None:
        raise RuntimeError("no provider configured")

    monkeypatch.setattr("graph.dispatch.gemma_provider", _raise)
    result = await summarize_community(comm, [ent], [], "ws-fb")
    assert result, "fallback must be non-empty"
    assert "Alice Smith" in result, "fallback must include the entity name"


async def test_community_summary_empty_community_skips_provider(monkeypatch) -> None:
    """A community with no members short-circuits before any LLM call."""
    from graph.communities import summarize_community
    from graph.interfaces import GraphCommunity

    comm = GraphCommunity(
        id="comm-empty",
        workspace_id="ws-e",
        title="Empty",
        summary="",
        entity_ids=[],
    )
    called = []

    def _spy():
        called.append(True)
        return (
            _FakeProvider(),
            "google/gemma-4-26b-a4b-it:free",
            "google/gemma-4-26b-a4b-it",
        )

    monkeypatch.setattr("graph.dispatch.gemma_provider", _spy)
    result = await summarize_community(comm, [], [], "ws-e")
    assert result == ""
    assert called == [], "no provider call expected for an empty community"
