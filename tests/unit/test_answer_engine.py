"""Unit tests for the answer engine + citation verification (TASK-M4-002).

Covers the ticket's custom glue: prompt construction, structured-output
parsing, citation verification (exists AND in-ACL, no fabrication), and the
engine pipeline incl. the unparseable-output fallback.
"""

from __future__ import annotations

import json

import pytest
from answer.engine import AnswerEngine
from answer.models import Answer, AnswerStatus, Claim, ConfidenceLevel
from answer.prompts import build_prompt, parse_model_output
from answer.verification import ExtractiveGrounder, verify_claims
from core.auth import ACLScope
from harness_fakes import make_item
from models.interfaces import (
    GenerationRequest,
    GenerationResult,
    LLMProvider,
)
from retrieval.interfaces import RetrievalOrchestrator

pytestmark = pytest.mark.unit


def _items() -> list:
    return [
        make_item(
            "Alpha evidence about permissions",
            0.9,
            workspace_id="ws-a",
            document_id="doc-a",
            chunk_id="c-1",
            page=1,
            section="Intro",
        ),
        make_item(
            "Bravo secret phrase from another workspace",
            0.8,
            workspace_id="ws-b",
            document_id="doc-b",
            chunk_id="c-2",
            page=2,
            section="Secret",
        ),
    ]


class _FakeOrchestrator(RetrievalOrchestrator):
    def __init__(self, items):
        self.items = items
        self.calls = 0

    async def retrieve(self, **kwargs):
        self.calls += 1
        return list(self.items)


class _FakeLLM(LLMProvider):
    provider_name = "fake-answer"

    def __init__(self, text):
        self.text = text
        self.requests = []

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        return GenerationResult(text=self.text, model=request.model)

    def stream(self, request: GenerationRequest):
        raise NotImplementedError


# --- prompt + parsing -------------------------------------------------------


def test_build_prompt_includes_chunk_ids_and_context() -> None:
    prompt = build_prompt("what governs access?", _items())
    assert "what governs access?" in prompt
    assert "c-1" in prompt and "c-2" in prompt
    assert "Alpha evidence about permissions" in prompt


def test_parse_model_output_plain_and_fenced() -> None:
    payload = (
        '{"answer": "Permissions are enforced.", '
        '"claims": [{"text": "ACL is enforced", "chunk_ids": ["c-1"]}]}'
    )
    parsed = parse_model_output(payload)
    assert parsed["answer"] == "Permissions are enforced."
    assert parsed["claims"] == [{"text": "ACL is enforced", "chunk_ids": ["c-1"]}]

    fenced = "```json\n" + payload + "\n```"
    assert parse_model_output(fenced)["answer"] == "Permissions are enforced."


def test_parse_model_output_tolerates_trailing_text() -> None:
    text = 'Here is the result: {"answer": "x", "claims": []} hope that helps'
    assert parse_model_output(text)["answer"] == "x"


def test_parse_model_output_rejects_bad_shape() -> None:
    with pytest.raises(ValueError):
        parse_model_output("no json at all")
    with pytest.raises(ValueError):
        parse_model_output('{"claims": []}')  # no answer
    with pytest.raises(ValueError):
        parse_model_output('{"answer": "x", "claims": "nope"}')


def test_parse_model_output_drops_malformed_claims() -> None:
    text = (
        '{"answer": "x", "claims": ['
        '{"text": "ok", "chunk_ids": ["c-1"]}, {"chunk_ids": []}, "junk"]}'
    )
    parsed = parse_model_output(text)
    assert parsed["claims"] == [{"text": "ok", "chunk_ids": ["c-1"]}]


# --- verification -----------------------------------------------------------


async def test_verify_claims_strips_missing_and_out_of_scope() -> None:
    items = _items()
    claims = [
        Claim(text="grounded", chunk_ids=["c-1", "missing", "c-2"]),  # c-2 is ws-b
        Claim(text="only foreign", chunk_ids=["c-2"]),
    ]
    verified, citations = await verify_claims(claims, items, allowed_workspace_ids={"ws-a"})
    assert len(verified) == 1
    assert verified[0].chunk_ids == ["c-1"]
    assert [c.chunk_id for c in citations] == ["c-1"]
    assert citations[0].document_id == "doc-a"
    assert citations[0].page == 1
    assert citations[0].section == "Intro"


async def test_verify_claims_span_is_computed() -> None:
    items = _items()
    claims = [Claim(text="permissions", chunk_ids=["c-1"])]
    _, citations = await verify_claims(claims, items)
    assert citations[0].span == (21, 32)  # "permissions" inside the chunk text


async def test_verify_claims_store_lookup_scoped_positive() -> None:
    """Lookup-resolved citation with a matching workspace is accepted."""

    class _Lookup:
        async def get(self, chunk_id):
            from answer.models import Citation

            return Citation(
                chunk_id=chunk_id,
                document_id="doc-c",
                page=3,
                workspace_id="ws-a",
            )

    items = _items()
    claims = [Claim(text="x", chunk_ids=["c-9"])]
    verified, citations = await verify_claims(
        claims, items, chunk_lookup=_Lookup(), allowed_workspace_ids={"ws-a"}
    )
    assert verified[0].chunk_ids == ["c-9"]
    assert citations[0].workspace_id == "ws-a"


async def test_verify_claims_store_lookup_foreign_lookup_negative() -> None:
    """Foreign lookup citation (different workspace) is rejected — negative pin."""

    class _ForeignLookup:
        async def get(self, chunk_id):
            from answer.models import Citation

            return Citation(
                chunk_id=chunk_id,
                document_id="doc-other",
                page=4,
                workspace_id="ws-b",
            )

    items = _items()
    claims = [Claim(text="x", chunk_ids=["c-9"])]
    verified, citations = await verify_claims(
        claims, items, chunk_lookup=_ForeignLookup(), allowed_workspace_ids={"ws-a"}
    )
    # Foreign workspace chunk is stripped; claim has zero valid ids -> dropped.
    assert verified == []
    assert citations == []


# --- engine -----------------------------------------------------------------


def _engine(llm, items=None) -> AnswerEngine:
    return AnswerEngine(
        orchestrator=_FakeOrchestrator(items if items is not None else _items()),
        llm=llm,
        model="fake-model",
    )


async def test_engine_returns_verified_answer() -> None:
    llm = _FakeLLM(
        '{"answer": "Permissions are enforced.", '
        '"claims": [{"text": "Alpha evidence about permissions", "chunk_ids": ["c-1", "c-2"]}]}'
    )
    result = await _engine(llm).answer(
        query="what governs access?", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert isinstance(result, Answer)
    assert result.answer == "Alpha evidence about permissions"  # rendered from verified claim only
    # c-2 is out-of-scope -> stripped; claim survives with c-1 only
    assert result.claims[0].chunk_ids == ["c-1"]
    assert [c.chunk_id for c in result.citations] == ["c-1"]
    # the prompt the model saw carried the retrieved context
    user_message = llm.requests[0].messages[-1]
    assert "c-1" in user_message.content


async def test_engine_unparseable_output_falls_back() -> None:
    from core.logging import reset_request_id, set_request_id

    token = set_request_id("trace-fallback-111")
    try:
        llm = _FakeLLM("I cannot answer that.")
        result = await _engine(llm).answer(
            query="q", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
        )
        # Raw prose must not be echoed as a factual answer; engine abstains.
        assert result.answer == "I cannot answer this question based on the provided documents."
        assert result.status == AnswerStatus.INSUFFICIENT_EVIDENCE
        assert result.reason_code == "PARSE_FALLBACK"
        assert result.trace_id == "trace-fallback-111"
        assert result.claims == [] and result.citations == []
    finally:
        reset_request_id(token)


async def test_engine_refuses_when_all_claims_rejected() -> None:
    llm = _FakeLLM(
        '{"answer": "Fabricated fact 999 days.", '
        '"claims": [{"text": "Fabricated fact", "chunk_ids": ["hallucinated-chunk-999"]}]}'
    )
    result = await _engine(llm).answer(
        query="notice period?", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert result.answer == "I cannot answer this question based on the provided documents."
    assert result.claims == []
    assert result.citations == []


async def test_engine_propagates_llm_error() -> None:
    from models.adapters.litellm_provider import LLMProviderError

    class _Down(LLMProvider):
        provider_name = "down"

        async def generate(self, request):
            raise LLMProviderError("gateway down")

        def stream(self, request):
            raise NotImplementedError

    with pytest.raises(LLMProviderError):
        await _engine(_Down()).answer(
            query="q", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
        )


async def test_engine_partial_claims_marks_partial_status() -> None:
    # One valid claim (c-1), one invalid claim (c-fake)
    llm = _FakeLLM(
        '{"answer": "Some info verified, some unverified.", '
        '"claims": ['
        '  {"text": "Alpha evidence", "chunk_ids": ["c-1"]},'
        '  {"text": "Unverified claim", "chunk_ids": ["c-fake"]}'
        ']}'
    )
    result = await _engine(llm).answer(
        query="test partial", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert result.status == "partial"
    assert result.reason_code == "PARTIAL_EVIDENCE"
    assert result.confidence.level == "medium"
    assert len(result.claims) == 1
    assert result.claims[0].text == "Alpha evidence"
    # Answer must be composed solely of verified claim text — unsupported
    # model prose is dropped, not echoed.
    assert result.answer == "Alpha evidence"


async def test_engine_empty_claims_not_answered() -> None:
    """Valid JSON with answer text but an empty claims list must abstain."""
    llm = _FakeLLM('{"answer": "The capital of France is Paris.", "claims": []}')
    result = await _engine(llm).answer(
        query="what is the capital?", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert result.status == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code == "NO_CLAIMS"
    assert result.answer == "I cannot answer this question based on the provided documents."
    assert result.claims == [] and result.citations == []


async def test_engine_malformed_prose_abstains() -> None:
    """Raw prose (not JSON, not a refusal phrase) must not be echoed as fact."""
    llm = _FakeLLM("The meeting is scheduled for Thursday the 12th of never.")
    result = await _engine(llm).answer(
        query="when is the meeting?", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert result.status == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code == "PARSE_FALLBACK"
    assert result.answer == "I cannot answer this question based on the provided documents."
    assert result.claims == [] and result.citations == []


async def test_engine_no_retrieved_items_abstains() -> None:
    """With zero retrieved items, even valid claims cannot be verified → abstain."""
    llm = _FakeLLM(
        '{"answer": "The deadline is Friday.", '
        '"claims": [{"text": "Deadline is Friday", "chunk_ids": ["c-1"]}]}'
    )
    result = await _engine(llm, items=[]).answer(
        query="when is the deadline?", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert result.status == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code == "EVIDENCE_MISSING"
    assert result.claims == [] and result.citations == []


async def test_engine_trace_id_in_parse_fallback() -> None:
    """trace_id must be attached even when model output is unparseable."""
    from core.logging import reset_request_id, set_request_id

    token = set_request_id("trace-parse-fallback-789")
    try:
        llm = _FakeLLM("not even json")
        result = await _engine(llm).answer(
            query="q", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
        )
        assert result.trace_id == "trace-parse-fallback-789"
        assert result.status == AnswerStatus.INSUFFICIENT_EVIDENCE
    finally:
        reset_request_id(token)


async def test_engine_trace_id_in_no_claims_abstain() -> None:
    """trace_id must be attached when model returns empty claims."""
    from core.logging import reset_request_id, set_request_id

    token = set_request_id("trace-no-claims-456")
    try:
        llm = _FakeLLM('{"answer": "Some fact.", "claims": []}')
        result = await _engine(llm).answer(
            query="q", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
        )
        assert result.trace_id == "trace-no-claims-456"
        assert result.reason_code == "NO_CLAIMS"
    finally:
        reset_request_id(token)

async def test_engine_trace_id_attached() -> None:
    from core.logging import reset_request_id, set_request_id

    token = set_request_id("trace-abc-123")
    try:
        llm = _FakeLLM(
            '{"answer": "Answer text.", "claims": [{"text": "Claim", "chunk_ids": ["c-1"]}]}'
        )
        result = await _engine(llm).answer(
            query="test trace", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
        )
        assert result.trace_id == "trace-abc-123"
    finally:
        reset_request_id(token)


# --- reviewer regression: unsupported prose never returned (Codex re-review) --


@pytest.mark.parametrize("case", ["no_evidence", "all_claims_valid", "refusal_phrase"])
async def test_unsupported_prose_never_returned(case: str) -> None:
    """Every raw-prose return path must be replaced by abstention or verified
    claim text — never the model's ungrounded ``answer`` field."""
    unsupported = "Unsupported deadline is Friday."
    payload: dict[str, object] = {"answer": unsupported, "claims": []}
    kwargs: dict[str, object] = {}

    if case == "no_evidence":
        # Zero retrieved items: must not echo the unsupported answer.
        kwargs["items"] = []
    elif case == "all_claims_valid":
        # Model gives one valid claim (c-1) but unrelated unsupported answer.
        payload["claims"] = [{"text": "Alpha evidence about permissions", "chunk_ids": ["c-1"]}]
    else:
        # Answer starts with a refusal phrase but still embeds unsupported prose.
        payload["answer"] = "Insufficient information. " + unsupported

    result = await _engine(_FakeLLM(json.dumps(payload)), **kwargs).answer(
        query="q", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert unsupported not in result.answer


# --- v1-rereview Q5: semantic claim grounding + honest confidence ---


async def test_verify_claims_grounding_rejects_unsubstantiated_citation() -> None:
    """A claim citing a real, in-scope chunk that does NOT support it is dropped."""
    items = _items()
    grounder = ExtractiveGrounder()
    claims = [Claim(text="The capital of France is Paris.", chunk_ids=["c-1"])]
    verified, citations = await verify_claims(
        claims,
        items,
        allowed_workspace_ids={"ws-a"},
        claim_grounder=grounder,
    )
    assert verified == []
    assert citations == []


async def test_verify_claims_grounding_accepts_substantiated_citation() -> None:
    """A claim substantiated by the chunk survives with a grounding score."""
    items = _items()
    grounder = ExtractiveGrounder()
    claims = [Claim(text="Alpha evidence about permissions", chunk_ids=["c-1"])]
    verified, citations = await verify_claims(
        claims,
        items,
        allowed_workspace_ids={"ws-a"},
        claim_grounder=grounder,
    )
    assert len(verified) == 1 and verified[0].chunk_ids == ["c-1"]
    assert len(citations) == 1
    assert citations[0].chunk_id == "c-1"
    assert citations[0].grounding_score is not None
    assert citations[0].grounding_score == 1.0


async def test_verify_claims_no_grounder_keeps_legacy_behavior() -> None:
    """With no grounder, existence+scope only — unsubstantiated claims survive."""
    items = _items()
    claims = [Claim(text="The capital of France is Paris.", chunk_ids=["c-1"])]
    verified, citations = await verify_claims(
        claims, items, allowed_workspace_ids={"ws-a"}
    )
    assert len(verified) == 1 and verified[0].chunk_ids == ["c-1"]
    assert len(citations) == 1
    assert citations[0].grounding_score is None


async def test_engine_rejects_ungrounded_claim_citing_real_chunk() -> None:
    """Re-review scenario: a real, in-scope citation that does not support the
    claim must NOT yield ANSWERED/HIGH — it is rejected as insufficient."""
    llm = _FakeLLM(
        '{"answer": "The capital of France is Paris.", '
        '"claims": [{"text": "The capital of France is Paris.", "chunk_ids": ["c-1"]}]}'
    )
    result = await _engine(llm).answer(
        query="what is the capital of france?",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
    )
    assert result.status == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code == "CITATION_INVALID"
    assert result.claims == [] and result.citations == []
    assert result.confidence.level == ConfidenceLevel.LOW
    # The unsupported / fabricated answer text must not be echoed to the user.
    assert "paris" not in result.answer.lower()


async def test_engine_grounds_real_claim_high_confidence() -> None:
    """A claim substantiated by its cited chunk yields ANSWERED with HIGH conf."""
    llm = _FakeLLM(
        '{"answer": "Alpha evidence about permissions.", '
        '"claims": [{"text": "Alpha evidence about permissions", "chunk_ids": ["c-1"]}]}'
    )
    result = await _engine(llm).answer(
        query="what is the evidence?",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
    )
    assert result.status == AnswerStatus.ANSWERED
    assert result.confidence.level == ConfidenceLevel.HIGH
    assert len(result.citations) == 1
    assert result.citations[0].grounding_score is not None
    assert result.citations[0].grounding_score >= 0.8


# --- v1-rereview Q5 P1: token-overlap cannot separate support from contradictions ---


async def test_verify_claims_extractive_rejects_negation_reversal() -> None:
    """'The launch is approved.' is NOT a substring of 'The launch is not approved.'
    Token-overlap would score this 1.0; verbatim extractive grounding rejects it."""
    items = [make_item("The launch is not approved.", 0.9, chunk_id="c-con")]
    claims = [Claim(text="The launch is approved.", chunk_ids=["c-con"])]
    verified, citations = await verify_claims(
        claims, items, allowed_workspace_ids={"ws-a"},
        claim_grounder=ExtractiveGrounder(),
    )
    assert verified == [] and citations == []


async def test_verify_claims_extractive_rejects_entity_role_swap() -> None:
    """'Bob paid Alice.' is a token-identical reorder of 'Alice paid Bob.' —
    rejected by verbatim containment, accepted by token overlap."""
    items = [make_item("Alice paid Bob.", 0.9, chunk_id="c-swap")]
    claims = [Claim(text="Bob paid Alice.", chunk_ids=["c-swap"])]
    verified, citations = await verify_claims(
        claims, items, allowed_workspace_ids={"ws-a"},
        claim_grounder=ExtractiveGrounder(),
    )
    assert verified == [] and citations == []


async def test_verify_claims_extractive_rejects_quantity_change() -> None:
    """'...900 dollars.' shares 8/9 tokens with '...100 dollars.' but is not a
    substring; the changed value (the entire point) is missed by token overlap."""
    src = "The approved budget for the project is 100 dollars."
    claim = "The approved budget for the project is 900 dollars."
    items = [make_item(src, 0.9, chunk_id="c-budget")]
    verified, citations = await verify_claims(
        [Claim(text=claim, chunk_ids=["c-budget"])], items,
        allowed_workspace_ids={"ws-a"}, claim_grounder=ExtractiveGrounder(),
    )
    assert verified == [] and citations == []


async def test_engine_abstains_on_negation_contradiction() -> None:
    """End-to-end: a real, in-scope chunk whose text contradicts the claim must
    NOT yield ANSWERED/HIGH, and the false claim must not be echoed."""
    items = [make_item("The launch is not approved.", 0.9, chunk_id="c-1")]
    llm = _FakeLLM(
        '{"answer": "The launch is approved.", '
        '"claims": [{"text": "The launch is approved.", "chunk_ids": ["c-1"]}]}'
    )
    result = await _engine(llm, items=items).answer(
        query="is the launch approved?",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
    )
    assert result.status == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code == "CITATION_INVALID"
    assert result.claims == [] and result.citations == []
    assert result.confidence.level == ConfidenceLevel.LOW
    assert "launch is approved" not in result.answer.lower()


# --- v1-rereview Q5 P1 (rerereview): context negation / conditionals / partial numbers ---
# Bare substring matching still fails when the claim clause appears verbatim
# *inside* a negated / conditional / partial-numeric sentence. The
# sentence-aware grounder must abstain in all three, while still grounding the
# exact value in a clean sentence.

@pytest.mark.parametrize(
    "src,claim",
    [
        # clause-level negation: "It is false that X" still contains "X" verbatim
        ("It is false that the launch is approved.", "The launch is approved."),
        # conditional: the antecedent "X" is not asserted as fact
        ("If the launch is approved, notify Alice.", "The launch is approved."),
        # partial numeric: "900" is a sub-value of "1900" (token-boundary guard)
        ("The budget is 1900 dollars.", "900 dollars."),
    ],
)
async def test_verify_claims_abstains_on_contextual_unsupported(src: str, claim: str) -> None:
    items = [make_item(src, 0.9, chunk_id="c-1")]
    verified, citations = await verify_claims(
        [Claim(text=claim, chunk_ids=["c-1"])], items,
        allowed_workspace_ids={"ws-a"}, claim_grounder=ExtractiveGrounder(),
    )
    assert verified == [] and citations == []


@pytest.mark.parametrize(
    "src,claim",
    [
        ("It is false that the launch is approved.", "The launch is approved."),
        ("If the launch is approved, notify Alice.", "The launch is approved."),
        ("The budget is 1900 dollars.", "900 dollars."),
    ],
)
async def test_engine_abstains_on_contextual_unsupported(src: str, claim: str) -> None:
    """End-to-end: a verbatim-but-negated/conditional/partial-numeric claim citing
    a real in-scope chunk must NOT yield ANSWERED/HIGH; the false claim is not echoed."""
    items = [make_item(src, 0.9, chunk_id="c-1")]
    llm = _FakeLLM(
        json.dumps({"answer": claim, "claims": [{"text": claim, "chunk_ids": ["c-1"]}]})
    )
    result = await _engine(llm, items=items).answer(
        query="q",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
    )
    assert result.status == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code == "CITATION_INVALID"
    assert result.claims == [] and result.citations == []
    assert result.confidence.level == ConfidenceLevel.LOW
    assert claim.lower() not in result.answer.lower()


async def test_engine_grounds_exact_value_not_partial_numeric() -> None:
    """Precision control: the exact value '100 dollars' in a clean sentence is
    grounded HIGH (word-boundary match, no negation/conditional), proving the
    partial-numeric guard is not blunt."""
    items = [make_item("The budget is 100 dollars.", 0.9, chunk_id="c-1")]
    claim = "The budget is 100 dollars."
    llm = _FakeLLM(
        json.dumps({"answer": claim, "claims": [{"text": claim, "chunk_ids": ["c-1"]}]})
    )
    result = await _engine(llm, items=items).answer(
        query="q", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert result.status == AnswerStatus.ANSWERED
    assert result.confidence.level == ConfidenceLevel.HIGH
    assert len(result.citations) == 1
    assert result.citations[0].grounding_score == 1.0


# --- v1-rereview Q5 P2: cross-language translation is NOT supported by v1 ---
# Extractive grounding requires the claim text to be a substring of the chunk.
# A translation's text is in a different language, so it can never be a substring
# of the source. The engine therefore correctly abstains instead of granting a
# false HIGH. This is a DISCLOSED v1 INTERIM LIMITATION: the multilingual
# (EN/RO/PT) cross-language release gate is NOT met by this mode.


@pytest.mark.parametrize(
    "translation",
    [
        "Capitala Franței este Paris.",  # correct RO translation
        "A capital da França é Paris.",  # correct PT translation
        "Capitala României este Paris.",  # incorrect RO (Bucharest, not France)
    ],
)
async def test_engine_cross_language_translation_abstains(translation: str) -> None:
    items = [make_item("The capital of France is Paris.", 0.9, chunk_id="c-1")]
    llm = _FakeLLM(
        json.dumps(
            {
                "answer": translation,
                "claims": [{"text": translation, "chunk_ids": ["c-1"]}],
            }
        )
    )
    result = await _engine(llm, items=items).answer(
        query="care este capitala?",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
    )
    assert result.status == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code == "CITATION_INVALID"
    assert result.claims == [] and result.citations == []
    assert result.confidence.level == ConfidenceLevel.LOW
    # The unverifiable translation must not be echoed to the user as fact.
    assert translation.lower() not in result.answer.lower()

