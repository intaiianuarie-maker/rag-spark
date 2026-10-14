"""T-V1-022 — bulk canonical hydration before context build (RP-03 / QF-03).

Controls covered here:

* **hydration precedes context AND verify** — the orchestrator seam returns
  canonical passages, so the prompt the model reads never contains a retrieved
  summary or payload echo, and verification grounds on the same canonical text;
* **summaries/payload text never override canonical** — a ref whose store
  record says "20" can never substantiate a claim of "900" no matter what the
  retrieved item carried; a ref that does not resolve (missing / deleted /
  superseded / foreign) is rejected even when a retrieved item carries it;
* **graph descriptions stay routing hints, never cited** — marked
  ``routing_hint`` by ``graph.retriever``, dropped from context when they
  cannot be hydrated, and never a grounding source of their own;
* **lifecycle revalidation just before returning** — a document deleted while
  the model was generating loses its citations at the door;
* **metrics** — source lookups on the verify path > 0; unverified refs cited: 0.
"""

from __future__ import annotations

import json

import pytest
from answer.adapters.postgres_lookup import MemoryChunkLookup
from answer.engine import AnswerEngine
from answer.hydration import (
    CanonicalHydrator,
    HydratingOrchestrator,
    allowed_workspace_ids_for,
    is_routing_hint,
)
from answer.models import AnswerStatus, Claim
from answer.verification import ExtractiveGrounder, verify_claims
from core.auth import ACLScope
from graph.retriever import GraphRetriever
from harness_fakes import make_item
from models.interfaces import (
    GenerationRequest,
    GenerationResult,
    LLMProvider,
    MessageRole,
)
from retrieval.interfaces import (
    RetrievalMethod,
    RetrievalOrchestrator,
    RetrievedItem,
)

pytestmark = pytest.mark.unit

WS = "ws-a"
OTHER_WS = "ws-foreign"
GRAPH_SUMMARY = (
    "[Knowledge Graph Context]\n"
    "Entities: Launch (EVENT). Relationships: approves budget of 900 dollars."
)
CANONICAL = "The team met to review expenses. The launch budget is 20 dollars."


def _store(*, include_deleted: bool = False, include_foreign: bool = False) -> MemoryChunkLookup:
    lookup = MemoryChunkLookup()
    lookup.add_chunk(
        "ch-g",
        content=CANONICAL,
        workspace_id=WS,
        document_id="doc-launch",
        filename="launch_plan.pdf",
        page=1,
        section="Budget",
    )
    lookup.add_chunk(
        "ch-d",
        content=CANONICAL,
        workspace_id=WS,
        document_id="doc-launch",
        filename="launch_plan.pdf",
        page=1,
        section="Budget",
    )
    if include_deleted:
        lookup.add_chunk("ch-dead", content=CANONICAL, workspace_id=WS, is_deleted=True)
    if include_foreign:
        lookup.add_chunk(
            "ch-foreign",
            content="Bravo secret phrase from another workspace",
            workspace_id=OTHER_WS,
        )
    return lookup


def _graph_item(chunk_id: str = "ch-g") -> RetrievedItem:
    return RetrievedItem(
        content=GRAPH_SUMMARY,
        score=1.0,
        method=RetrievalMethod.FUSED,
        workspace_id=WS,
        chunk_id=chunk_id,
        metadata={"is_graph": True, "routing_hint": True},
    )


def _payload_item(chunk_id: str = "ch-d") -> RetrievedItem:
    return RetrievedItem(
        content="Retrieved payload: the launch budget is 900 dollars.",
        score=0.9,
        method=RetrievalMethod.DENSE,
        workspace_id=WS,
        document_id="doc-launch",
        chunk_id=chunk_id,
        metadata={"filename": "launch_plan.pdf"},
    )


class _FakeOrchestrator(RetrievalOrchestrator):
    def __init__(self, items):
        self.items = items
        self.last_kwargs: dict = {}

    async def retrieve(self, **kwargs):
        self.last_kwargs = dict(kwargs)
        return list(self.items)


class _FakeLLM(LLMProvider):
    provider_name = "fake"

    def __init__(self, text: str):
        self.text = text
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        return GenerationResult(text=self.text, model=request.model)

    def stream(self, request: GenerationRequest):
        raise NotImplementedError


def _user_prompt(llm: _FakeLLM) -> str:
    messages = llm.requests[0].messages
    return next(m.content for m in messages if m.role is MessageRole.USER)


# --- context seam: hydration precedes context build -------------------------


async def test_context_seam_hydrates_before_build_prompt() -> None:
    """The prompt carries canonical passages, never a summary/payload echo."""
    lookup = _store(include_deleted=True, include_foreign=True)
    hydrator = CanonicalHydrator(lookup)
    inner = _FakeOrchestrator(
        [
            _graph_item(),  # description says 900
            _payload_item(),  # payload says 900
            _graph_item("ch-dead"),  # deleted in the store
            _graph_item("ch-foreign"),  # other workspace
            RetrievedItem(  # summary-only: no ref at all
                content=GRAPH_SUMMARY,
                score=0.9,
                method=RetrievalMethod.FUSED,
                workspace_id=WS,
                metadata={"is_graph": True, "routing_hint": True},
            ),
            _payload_item("ch-d"),  # duplicate ref → collapses to one block
        ]
    )
    orchestrator = HydratingOrchestrator(inner, hydrator)

    items = await orchestrator.retrieve(
        query="what is the budget?",
        acl_scope=ACLScope(user_id="u1", workspace_ids=[WS]),
    )

    # One authoritative block per resolvable ref; hints/deleted/foreign dropped.
    assert [i.chunk_id for i in items] == ["ch-g", "ch-d"]
    for item in items:
        assert item.content == CANONICAL
        assert "900" not in item.content
        assert "[Knowledge Graph Context]" not in item.content
        assert item.metadata["canonical"] is True
        assert item.document_id == "doc-launch"
        assert item.page == 1
        assert item.section == "Budget"
        assert item.workspace_id == WS

    # Metric: the context seam actually read the store (bulk, deduplicated):
    # ch-g, ch-d, ch-dead and the foreign ref (read, then scope-refused).
    assert hydrator.lookups == 4
    assert allowed_workspace_ids_for(ACLScope(user_id="u1", workspace_ids=[WS])) == {WS}
    assert allowed_workspace_ids_for(ACLScope(user_id="u1", include_public=True)) is None


async def test_engine_prompt_is_canonical_and_answer_is_verified() -> None:
    """End to end: hydration runs BEFORE the prompt is built and BEFORE verify."""
    lookup = _store()
    hydrator = CanonicalHydrator(lookup)
    llm = _FakeLLM(
        json.dumps(
            {
                "answer": "The launch budget is 20 dollars.",
                "claims": [{"text": "The launch budget is 20 dollars.", "chunk_ids": ["ch-g"]}],
            }
        )
    )
    engine = AnswerEngine(
        orchestrator=HydratingOrchestrator(
            _FakeOrchestrator([_graph_item(), _payload_item()]), hydrator
        ),
        llm=llm,
        model="fake-model",
        chunk_lookup=hydrator,
        claim_grounder=ExtractiveGrounder(),
    )

    answer = await engine.answer(
        query="what is the budget?",
        acl_scope=ACLScope(user_id="u1", workspace_ids=[WS]),
    )

    prompt = _user_prompt(llm)
    # Context seam: the model never saw the 900-dollar description or payload.
    assert "[Knowledge Graph Context]" not in prompt
    assert "900" not in prompt
    assert CANONICAL in prompt
    assert "ch-g" in prompt and "ch-d" in prompt

    # Verify seam: the citation is canonical, in-scope, grounded.
    assert answer.status == AnswerStatus.ANSWERED
    assert [c.chunk_id for c in answer.citations] == ["ch-g"]
    assert answer.citations[0].content == CANONICAL
    assert answer.citations[0].filename == "launch_plan.pdf"
    assert answer.citations[0].grounding_score == 1.0
    assert "20 dollars" in answer.answer
    assert hydrator.lookups > 0


async def test_context_seam_is_pass_through_without_a_store() -> None:
    """No store → v1 behaviour: items untouched, no reads issued."""
    hydrator = CanonicalHydrator(None)
    items = [_payload_item()]
    inner = _FakeOrchestrator(items)
    out = await HydratingOrchestrator(inner, hydrator).retrieve(
        query="q", acl_scope=ACLScope(user_id="u1", workspace_ids=[WS])
    )
    assert out == items
    assert hydrator.lookups == 0
    assert not hydrator.available


# --- verify seam: bulk hydration, payload never overrides, reject controls --


class _CountingLookup(MemoryChunkLookup):
    """MemoryChunkLookup that records every store read (the T-V1-022 metric)."""

    def __init__(self, records=None):
        super().__init__(records)
        self.calls = 0

    async def get(self, chunk_id: str):
        self.calls += 1
        return await super().get(chunk_id)


async def test_verify_bulk_hydration_reads_the_store_for_in_context_refs() -> None:
    """Failing control: an in-context ref used to skip the store entirely
    (0 lookups), so its payload stood in for the canonical record. Now every
    cited ref is resolved, and a ref the store refuses is rejected."""
    lookup = _CountingLookup()
    lookup.add_chunk("ch-live", content=CANONICAL, workspace_id=WS)
    lookup.add_chunk("ch-dead", content=CANONICAL, workspace_id=WS, is_superseded=True)
    items = [
        make_item(CANONICAL, 0.9, workspace_id=WS, chunk_id="ch-live"),
        make_item(CANONICAL, 0.9, workspace_id=WS, chunk_id="ch-dead"),
    ]
    claims = [
        Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-live"]),
        Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-dead"]),
    ]

    verified, citations = await verify_claims(
        claims,
        items,
        chunk_lookup=lookup,
        allowed_workspace_ids={WS},
        claim_grounder=ExtractiveGrounder(),
    )

    assert lookup.calls > 0  # before: 0 on this control
    assert [c.chunk_id for c in citations] == ["ch-live"]
    assert [c.chunk_ids for c in verified] == [["ch-live"]]
    # Superseded ref was IN the retrieved context and still got rejected.
    assert "ch-dead" not in {c.chunk_id for c in citations}


async def test_retrieved_payload_never_overrides_canonical_text() -> None:
    """Item says 900, store says 20: the store wins in both directions."""
    lookup = MemoryChunkLookup()
    lookup.add_chunk("ch-d", content=CANONICAL, workspace_id=WS)
    items = [_payload_item()]  # payload: "900 dollars"

    _verified_ok, citations_ok = await verify_claims(
        [Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-d"])],
        items,
        chunk_lookup=lookup,
        allowed_workspace_ids={WS},
        claim_grounder=ExtractiveGrounder(),
    )
    assert [c.chunk_id for c in citations_ok] == ["ch-d"]
    assert citations_ok[0].content == CANONICAL

    verified_bad, citations_bad = await verify_claims(
        [Claim(text="the launch budget is 900 dollars.", chunk_ids=["ch-d"])],
        items,
        chunk_lookup=lookup,
        allowed_workspace_ids={WS},
        claim_grounder=ExtractiveGrounder(),
    )
    assert verified_bad == [] and citations_bad == []


@pytest.mark.parametrize(
    "chunk_id,records",
    [
        ("ch-missing", None),  # never existed
        ("ch-dead", "deleted"),
        ("ch-old", "superseded"),
        ("ch-foreign", "foreign"),
    ],
    ids=["missing", "deleted", "superseded", "foreign"],
)
async def test_reject_controls_never_cite_unresolvable_refs(chunk_id, records) -> None:
    """Every unresolvable ref is rejected even when a retrieved item carries it."""
    lookup = MemoryChunkLookup()
    if records == "deleted":
        lookup.add_chunk(chunk_id, content=CANONICAL, workspace_id=WS, is_deleted=True)
    elif records == "superseded":
        lookup.add_chunk(chunk_id, content=CANONICAL, workspace_id=WS, is_superseded=True)
    elif records == "foreign":
        lookup.add_chunk(chunk_id, content=CANONICAL, workspace_id=OTHER_WS)
    items = [make_item(CANONICAL, 0.9, workspace_id=WS, chunk_id=chunk_id)]
    claims = [Claim(text="The launch budget is 20 dollars.", chunk_ids=[chunk_id])]

    verified, citations = await verify_claims(
        claims,
        items,
        chunk_lookup=lookup,
        allowed_workspace_ids={WS},
        claim_grounder=ExtractiveGrounder(),
    )
    assert verified == [] and citations == []


async def test_routing_hint_is_never_a_grounding_source_of_its_own() -> None:
    """``routing_hint`` items behave like graph items: no canonical text → no citation."""
    lookup = MemoryChunkLookup()
    hint = RetrievedItem(
        content=GRAPH_SUMMARY,
        score=1.0,
        method=RetrievalMethod.FUSED,
        workspace_id=WS,
        chunk_id="ch-hint",
        metadata={"routing_hint": True},  # no is_graph key
    )
    assert is_routing_hint(hint)

    # Ref not in the store → the description cannot be cited.
    verified, citations = await verify_claims(
        [Claim(text="budget of 900 dollars.", chunk_ids=["ch-hint"])],
        [hint],
        chunk_lookup=lookup,
        allowed_workspace_ids={WS},
        claim_grounder=ExtractiveGrounder(),
    )
    assert verified == [] and citations == []

    # With a canonical record the ref is citable — but only from that text.
    lookup.add_chunk("ch-hint", content=CANONICAL, workspace_id=WS)
    verified, citations = await verify_claims(
        [Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-hint"])],
        [hint],
        chunk_lookup=lookup,
        allowed_workspace_ids={WS},
        claim_grounder=ExtractiveGrounder(),
    )
    assert [c.chunk_id for c in citations] == ["ch-hint"]
    assert citations[0].content == CANONICAL
    assert "900" not in (citations[0].content or "")


async def test_no_store_keeps_legacy_contract_but_still_blocks_hints() -> None:
    """``chunk_lookup=None`` → legacy existence/scope; graph/routing hints still uncitable."""
    items = [
        make_item("Alpha evidence about permissions", 0.9, workspace_id=WS, chunk_id="c-1"),
        _graph_item("c-2"),
    ]
    claims = [
        Claim(text="Alpha evidence about permissions", chunk_ids=["c-1"]),
        Claim(text="budget of 900 dollars.", chunk_ids=["c-2"]),
    ]
    verified, citations = await verify_claims(
        claims, items, allowed_workspace_ids={WS}, claim_grounder=ExtractiveGrounder()
    )
    assert [c.chunk_id for c in citations] == ["c-1"]
    assert [c.chunk_ids for c in verified] == [["c-1"]]


# --- pre-return lifecycle revalidation --------------------------------------


class _DeletesMidFlightLookup(MemoryChunkLookup):
    """Returns the canonical record on the hydration pass, then reports it gone.

    Simulates a document deleted between the bulk hydration pass and the
    pre-return revalidation (i.e. while the model was generating).
    """

    def __init__(self):
        super().__init__()
        self.add_chunk("ch-g", content=CANONICAL, workspace_id=WS, document_id="doc-launch")
        self.calls = 0

    async def get(self, chunk_id: str):
        self.calls += 1
        if self.calls > 1:
            return None  # deleted concurrently
        return await super().get(chunk_id)


async def test_lifecycle_revalidated_just_before_returning() -> None:
    """A chunk deleted while the model was generating loses at the door."""
    lookup = _DeletesMidFlightLookup()
    item = make_item(CANONICAL, 0.9, workspace_id=WS, chunk_id="ch-g")

    verified, citations = await verify_claims(
        [Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-g"])],
        [item],
        chunk_lookup=lookup,
        allowed_workspace_ids={WS},
        claim_grounder=ExtractiveGrounder(),
    )

    assert lookup.calls >= 2  # hydration pass + pre-return revalidation
    assert verified == [] and citations == []


async def test_stable_store_keeps_the_citation() -> None:
    """Positive control: without the mid-flight delete the citation survives."""
    lookup = _CountingLookup()
    lookup.add_chunk("ch-g", content=CANONICAL, workspace_id=WS, document_id="doc-launch")
    item = make_item(CANONICAL, 0.9, workspace_id=WS, chunk_id="ch-g")

    verified, citations = await verify_claims(
        [Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-g"])],
        [item],
        chunk_lookup=lookup,
        allowed_workspace_ids={WS},
        claim_grounder=ExtractiveGrounder(),
    )
    assert [c.chunk_ids for c in verified] == [["ch-g"]]
    assert [c.chunk_id for c in citations] == ["ch-g"]
    assert lookup.calls >= 2  # hydration + revalidation both read the store


async def test_shared_hydrator_counts_lookups_once_per_request() -> None:
    """Context seam + verify seam share one hydrator/counter (one store read each)."""
    lookup = _CountingLookup()
    lookup.add_chunk("ch-d", content=CANONICAL, workspace_id=WS, document_id="doc-launch")
    hydrator = CanonicalHydrator(lookup)
    inner = _FakeOrchestrator([_payload_item()])
    items = await HydratingOrchestrator(inner, hydrator).retrieve(
        query="q", acl_scope=ACLScope(user_id="u1", workspace_ids=[WS])
    )
    assert hydrator.lookups == 1

    await verify_claims(
        [Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-d"])],
        items,
        chunk_lookup=hydrator,
        allowed_workspace_ids={WS},
        claim_grounder=ExtractiveGrounder(),
    )
    # verify re-reads (fresh) and revalidates (fresh): 1 + 1 more.
    assert hydrator.lookups == 3
    assert lookup.calls == 3


# --- graph retriever: descriptions are routing hints ------------------------


async def test_graph_retriever_marks_descriptions_as_routing_hints() -> None:
    """graph.retriever tags every synthesized passage as a routing hint."""
    from graph.interfaces import Entity, EntityType, GraphSearchResult

    entity = Entity(
        id="e1",
        name="Launch",
        type=EntityType.EVENT,
        workspace_id=WS,
        chunk_ids=["ch-g"],
    )
    result = GraphSearchResult(
        entities=[entity],
        relationships=[],
        evidence_chunk_ids=["ch-g"],
        summary="Entities: Launch (EVENT). Relationships: budget of 900 dollars.",
        score=1.0,
    )
    items = GraphRetriever(store=None).to_retrieved_items(result, WS)  # type: ignore[arg-type]

    assert len(items) == 1
    assert is_routing_hint(items[0])
    assert items[0].metadata["is_graph"] is True
    assert items[0].metadata["routing_hint"] is True
    # The hint alone is never citable: without hydration it is rejected.
    verified, citations = await verify_claims(
        [Claim(text="budget of 900 dollars.", chunk_ids=["ch-g"])],
        items,
        chunk_lookup=MemoryChunkLookup(),
        allowed_workspace_ids={WS},
        claim_grounder=ExtractiveGrounder(),
    )
    assert verified == [] and citations == []


# --------------------------------------------------------------------------------------
# T-K02: lookup construction log/fail-closed + graph empty-content
# --------------------------------------------------------------------------------------


async def test_lookup_construction_failure_logs_warning_and_degrades(
    monkeypatch, caplog
) -> None:
    """T-K02: a lookup construction failure logs a warning with the exception
    type and degrades to ``chunk_lookup=None`` — never silent, engine still
    builds and answers.
    """
    import logging

    import answer.factory as factory_mod

    class _BoomLookup:
        def __init__(self) -> None:
            raise RuntimeError("simulated construction failure")

    monkeypatch.setattr(
        "answer.adapters.postgres_lookup.PostgresChunkLookup", _BoomLookup
    )
    caplog.set_level(logging.WARNING, logger="knowledge_os.answer.factory")

    with caplog.at_level(logging.WARNING, logger="knowledge_os.answer.factory"):
        engine = factory_mod.build_answer_engine(model="test-model")

    # Engine still builds (degradation, not a crash).
    assert engine is not None
    # chunk_lookup is None → legacy existence/scope-only verification path.
    assert engine.chunk_lookup is None

    # A warning was logged with the exception type — never silent.
    warnings = [
        r for r in caplog.records
        if getattr(r, "event", None) == "chunk_lookup_construction_failed"
    ]
    assert warnings, "expected chunk_lookup_construction_failed warning"
    assert warnings[0].error_type == "RuntimeError"


async def test_refless_graph_item_drops_before_prompt_build() -> None:
    """T-K02: a refless graph item (no chunk_id) drops before prompt build.

    The hydration seam drops items with no ref (they can never be cited), so
    the synthesized routing-hint text never reaches the model.
    """
    from answer.hydration import CanonicalHydrator

    refless = RetrievedItem(
        content="",
        score=1.0,
        method=RetrievalMethod.FUSED,
        workspace_id=WS,
        metadata={"is_graph": True, "routing_hint": True},
    )
    assert not refless.chunk_id

    hydrator = CanonicalHydrator(lookup=MemoryChunkLookup())
    hydrated = await hydrator.hydrate_items([refless], allowed_workspace_ids={WS})

    # Refless item dropped — never reaches the prompt.
    assert hydrated == []
