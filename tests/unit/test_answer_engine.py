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
        self.last_kwargs: dict = {}

    async def retrieve(self, **kwargs):
        self.calls += 1
        self.last_kwargs = dict(kwargs)
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


async def test_verify_claims_citation_carries_canonical_content() -> None:
    """F6: every verified citation carries the exact content it was grounded against."""
    items = _items()
    claims = [Claim(text="Alpha evidence about permissions", chunk_ids=["c-1"])]
    _, citations = await verify_claims(claims, items, allowed_workspace_ids={"ws-a"})
    assert len(citations) == 1
    assert citations[0].content == "Alpha evidence about permissions"


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


@pytest.mark.parametrize(
    "src",
    [
        # reporting verb in a conjunction clause after the claim (NOT attribution)
        "The launch is approved, and Bob responded.",
        # reporting verb in a subordinate clause after the claim (NOT attribution)
        "The launch is approved after Bob responded.",
        # reporting verb after a semicolon clause (NOT attribution)
        "The launch is approved; Bob responded by email.",
    ],
)
async def test_engine_postclaim_verb_not_attribution_is_grounded(src: str) -> None:
    """R9: A reporting verb appearing later in the same sentence, in a
    non-speaker-predicate clause (', and Bob responded', 'after Bob responded',
    '; Bob responded by email'), is NOT postposed attribution. Frame 4 only
    checks the first token of the post-claim text — a speaker predicate like
    ', said Alice' — so distant verbs do not trigger false abstention."""
    claim = "The launch is approved."
    assert ExtractiveGrounder().score(claim, src) == 1.0
    items = [make_item(src, 0.9, chunk_id="c-1")]
    llm = _FakeLLM(
        json.dumps({"answer": claim, "claims": [{"text": claim, "chunk_ids": ["c-1"]}]})
    )
    result = await _engine(llm, items=items).answer(
        query="q",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
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


# --- v1-rereview Q5 follow-up: attribution, decimal, and span regressions ------


@pytest.mark.parametrize(
    "attribution",
    [
        "denied",      # clause-level negation: Alice denied the claim
        "reported",    # third-party attribution: Alice reported the claim
        "qualified",   # hedged attribution: Alice qualified the claim
    ],
)
async def test_engine_denied_reported_qualified_attribution_abstains(
    attribution: str,
) -> None:
    """P1: A claim whose verbatim text appears inside a sentence where a third
    party denies, reports, or qualifies it must NOT be promoted to HIGH.
    The source is attributing or withholding the claim, not asserting it."""
    src = f"Alice {attribution} the claim: the launch is approved."
    claim = "The launch is approved."
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
    assert "launch is approved" not in result.answer.lower()


async def test_engine_valid_decimal_not_split() -> None:
    """P1 positive: The decimal 1.25 stays intact during sentence splitting.
    The full claim 'The rate is 1.25 percent.' is grounded HIGH."""
    src = "The rate is 1.25 percent."
    claim = "The rate is 1.25 percent."
    items = [make_item(src, 0.9, chunk_id="c-1")]
    llm = _FakeLLM(
        json.dumps({"answer": claim, "claims": [{"text": claim, "chunk_ids": ["c-1"]}]})
    )
    result = await _engine(llm, items=items).answer(
        query="q",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
    )
    assert result.status == AnswerStatus.ANSWERED
    assert result.confidence.level == ConfidenceLevel.HIGH
    assert len(result.citations) == 1
    assert result.citations[0].grounding_score == 1.0


async def test_engine_partial_decimal_value_rejected() -> None:
    """P1 negative: '25 percent.' is NOT a standalone value of '1.25 percent.'
    The numeric-boundary guard in the sentence splitter preserves the decimal."""
    src = "The rate is 1.25 percent."
    claim = "25 percent."
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
    assert "25 percent" not in result.answer.lower()


async def test_engine_citation_span_to_accepted_sentence() -> None:
    """P2: When the same claim text appears in a negated sentence (rejected) and
    a clean sentence (accepted), the citation span must point to the accepted
    sentence, not the first (rejected) occurrence."""
    src = "It is false that the launch is approved. The launch is approved."
    claim = "The launch is approved."
    items = [make_item(src, 0.9, chunk_id="c-1")]
    llm = _FakeLLM(
        json.dumps({"answer": claim, "claims": [{"text": claim, "chunk_ids": ["c-1"]}]})
    )
    result = await _engine(llm, items=items).answer(
        query="q",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
    )
    assert result.status == AnswerStatus.ANSWERED
    assert result.confidence.level == ConfidenceLevel.HIGH
    assert len(result.citations) == 1
    span = result.citations[0].span
    # Span must fall within the second sentence (starts at position 41):
    # "It is false that the launch is approved. " = 41 chars
    assert span[0] >= 41
    # The span text must be the exact claim verbatim.
    assert src[span[0] : span[1]].lower() == "the launch is approved"


# --- v1-rereview Q5 r4: structural quotation mode + original-offset spans ------


@pytest.mark.parametrize(
    "src",
    [
        # colon-introduced attributed content (contested assertion)
        "Alice challenged the assertion: the launch is approved.",
        # quoted material attributed to a third party
        "\u201cThe launch is approved,\u201d said Alice.",
    ],
)
async def test_engine_attributed_quotation_abstains(src: str) -> None:
    """R4 P1: a verbatim claim that is quoted or colon-introduced attributed
    content must NOT be promoted to HIGH. The grounder rejects the frame
    structurally (quotation mark / introducing colon) — a context-preserving
    quotation mode — rather than an open-ended attribution-verb denylist."""
    claim = "The launch is approved."
    assert ExtractiveGrounder().score(claim, src) == 0.0
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
    assert "launch is approved" not in result.answer.lower()


async def test_engine_unicode_before_match_citation_slice() -> None:
    """R4 P2: Unicode before the accepted sentence must not shift the citation
    span. casefold('Straße') expands to 'strasse' (+1 char); spans must be
    computed on the original text so the slice is the actual source span."""
    src = "Straße. The launch is approved."
    claim = "The launch is approved."
    # The occurrence starts at index 8 (after "Straße. "), not 9.
    assert ExtractiveGrounder().locate(claim, src) == (8, 30)
    items = [make_item(src, 0.9, chunk_id="c-1")]
    llm = _FakeLLM(
        json.dumps({"answer": claim, "claims": [{"text": claim, "chunk_ids": ["c-1"]}]})
    )
    result = await _engine(llm, items=items).answer(
        query="q",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
    )
    assert result.status == AnswerStatus.ANSWERED
    assert result.confidence.level == ConfidenceLevel.HIGH
    assert len(result.citations) == 1
    span = result.citations[0].span
    assert span is not None
    # Citation slice must equal the actual accepted source span.
    assert src[span[0]:span[1]] == "The launch is approved"


async def test_engine_unicode_in_match_citation_slice() -> None:
    """R4 P2: Unicode *inside* the matched claim must not shift the span end.
    The citation slice must equal the actual accepted source span, not a
    casefold-expanded offset."""
    src = "Die breite Straße ist frei."
    claim = "Straße ist frei."
    # The claim occurs at (11, 26) in the original text (ß stays one char).
    assert ExtractiveGrounder().locate(claim, src) == (11, 26)
    items = [make_item(src, 0.9, chunk_id="c-1")]
    llm = _FakeLLM(
        json.dumps({"answer": claim, "claims": [{"text": claim, "chunk_ids": ["c-1"]}]})
    )
    result = await _engine(llm, items=items).answer(
        query="q",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
    )
    assert result.status == AnswerStatus.ANSWERED
    assert result.confidence.level == ConfidenceLevel.HIGH
    assert len(result.citations) == 1
    span = result.citations[0].span
    assert span is not None
    assert src[span[0]:span[1]] == "Straße ist frei"


# --- v1-rereview Q5 r5: indirect attribution must abstain ----------------------


@pytest.mark.parametrize(
    "src",
    [
        # indirect speech: subject + reporting verb + content clause
        "Alice said the launch is approved.",
        # comma-fronted attribution: "According to <person>, <claim>"
        "According to Alice, the launch is approved.",
        # postposed attribution: "<claim>, said <person>"
        "The launch is approved, said Alice.",
        # direct attribution with restored verbs (was false-grounded in R8):
        "Alice reported the launch is approved.",
        "Alice noted the launch is approved.",
    ],
)
async def test_engine_indirect_attribution_abstains(src: str) -> None:
    """R5 P1: indirect-attribution constructions (no quotes, no colon) must
    NOT be promoted to HIGH. The grounder rejects them structurally — a comma
    before the claim (fronted attribution) or a reporting verb framing the
    claim (indirect speech) — as part of the context-preserving contract."""
    claim = "The launch is approved."
    assert ExtractiveGrounder().score(claim, src) == 0.0
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
    assert "launch is approved" not in result.answer.lower()


@pytest.mark.parametrize(
    "src",
    [
        # temporal phrase + comma is NOT attribution
        "On Friday, the launch is approved.",
        # prepositional phrase + comma is NOT attribution (noun "report",
        # not the reporting verb - "report" was removed from the verb class)
        "In the report, the launch is approved.",
        # prepositional phrase + comma is NOT attribution (noun "note",
        # also removed from the verb class)
        "In the note, the launch is approved.",
        # subordinate clause + comma: "responded" is a reporting verb but
        # it is in a clause introduced by "After" - a subordinator, not
        # attribution of the claim
        "After Bob responded, the launch is approved.",
    ],
)
async def test_engine_comma_not_attribution_is_grounded(src: str) -> None:
    """REREREVIEW-7/8: A comma before the claim is NOT by itself attribution.
    Temporal/prepositional phrases like 'On Friday, ...' and 'In the report/
    note, ...' must still be grounded HIGH when the claim is verbatim. A
    reporting verb in a subordinate clause ('After Bob responded, ...') is also
    NOT attribution. Frame 3 requires a reporting verb in the text before the
    comma that does NOT start with a subordinating conjunction; Frame 4 only
    checks the text immediately adjacent to the claim (after the last comma).
    Retains negatives for genuine attribution in
    test_engine_indirect_attribution_abstains above."""
    claim = "The launch is approved."
    assert ExtractiveGrounder().score(claim, src) == 1.0
    items = [make_item(src, 0.9, chunk_id="c-1")]
    llm = _FakeLLM(
        json.dumps({"answer": claim, "claims": [{"text": claim, "chunk_ids": ["c-1"]}]})
    )
    result = await _engine(llm, items=items).answer(
        query="q",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
    )
    assert result.status == AnswerStatus.ANSWERED
    assert result.confidence.level == ConfidenceLevel.HIGH
    assert len(result.citations) == 1
    assert result.citations[0].grounding_score == 1.0


# --- G4 wiring: mode passthrough + graph-citation safety ----------------------


async def test_engine_answer_defaults_to_hybrid_mode() -> None:
    """G4 §1b: default /ask (no mode) is byte-identical to today (HYBRID)."""
    from retrieval.interfaces import RetrievalMode

    llm = _FakeLLM(
        '{"answer": "Permissions are enforced.", '
        '"claims": [{"text": "Alpha evidence about permissions", "chunk_ids": ["c-1"]}]}'
    )
    engine = _engine(llm)
    result = await engine.answer(
        query="what governs access?", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert result.status == AnswerStatus.ANSWERED
    assert engine.orchestrator.last_kwargs.get("mode") == RetrievalMode.HYBRID


async def test_engine_answer_passes_mode_through_to_orchestrator() -> None:
    """G4 §1b: explicit mode reaches orchestrator.retrieve(mode=mode)."""
    from retrieval.interfaces import RetrievalMode

    llm = _FakeLLM(
        '{"answer": "Permissions are enforced.", '
        '"claims": [{"text": "Alpha evidence about permissions", "chunk_ids": ["c-1"]}]}'
    )
    engine = _engine(llm)
    await engine.answer(
        query="what governs access?",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
        mode=RetrievalMode.HYBRID_GRAPH,
    )
    assert engine.orchestrator.last_kwargs.get("mode") == RetrievalMode.HYBRID_GRAPH


async def test_engine_never_grounds_bare_synthesized_graph_context() -> None:
    """G4 §2a: a claim is never grounded against the synthesized passage.

    A graph-derived item whose content is ONLY the ``[Knowledge Graph
    Context]`` passage (no locatable claim span in original chunk text) is
    dropped by the extractive grounder, so the engine abstains rather than
    citing synthesized content.
    """
    claim = "The launch is approved."
    items = [
        make_item(
            "[Knowledge Graph Context]\nEntities: Launch (EVENT). Relationships: none",
            1.0,
            chunk_id="c-graph-1",
        )
    ]
    llm = _FakeLLM(
        json.dumps({"answer": claim, "claims": [{"text": claim, "chunk_ids": ["c-graph-1"]}]})
    )
    result = await _engine(llm, items=items).answer(
        query="q",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
    )
    assert ExtractiveGrounder().score(claim, items[0].content) == 0.0
    assert result.status == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code == "CITATION_INVALID"
    assert result.claims == [] and result.citations == []


async def test_engine_hydrated_graph_chunk_grounds_normally() -> None:
    """G4 §2a positive pin: authoritative original-chunk text grounds as usual.

    Graph provenance (``RetrievalMethod.GRAPH``) changes nothing about the
    grounding rule — a claim verbatim in real chunk text is still accepted.
    """
    from retrieval.interfaces import RetrievalMethod

    claim = "The launch is approved."
    items = [
        make_item(
            "Board minutes record the decision. The launch is approved.",
            1.0,
            chunk_id="c-graph-1",
            method=RetrievalMethod.GRAPH,
        )
    ]
    llm = _FakeLLM(
        json.dumps({"answer": claim, "claims": [{"text": claim, "chunk_ids": ["c-graph-1"]}]})
    )
    result = await _engine(llm, items=items).answer(
        query="q",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
    )
    assert result.status == AnswerStatus.ANSWERED
    assert result.citations[0].chunk_id == "c-graph-1"



# --- T-V1-002: the Citation.content guarantee is grounder-gated --------------


async def test_citation_content_guarantee_is_grounder_gated() -> None:
    """T-V1-002 (b): the ``Citation.content`` guarantee is scoped, and the
    field comment says exactly what the code does.

    ``content`` is populated on every grounder-gated path of ``verify_claims``
    (retrieved item text, store-hydrated canonical text for graph/lookup
    citations) — no grounder-verified citation is None. The legacy
    existence-only contract (``claim_grounder=None``) is the sole documented
    exception: it may verify a store lookup citation without hydrated content.
    """
    import inspect

    from answer.models import Citation

    comment = inspect.getsource(Citation)
    assert "grounder-gated" in comment
    assert "claim_grounder=None" in comment

    class _UnhydratedLookup:
        async def get(self, chunk_id):
            return Citation(
                chunk_id=chunk_id,
                document_id="doc-c",
                page=3,
                workspace_id="ws-a",
            )

    items = _items()
    claims = [Claim(text="x", chunk_ids=["c-9"])]

    # Legacy existence-only path: still verifies, content may be None.
    verified, citations = await verify_claims(
        claims, items, chunk_lookup=_UnhydratedLookup(), allowed_workspace_ids={"ws-a"}
    )
    assert verified[0].chunk_ids == ["c-9"]
    assert citations[0].content is None

    # Grounder-gated path: no canonical content -> citation dropped, so no
    # grounder-verified citation can carry content=None.
    verified_g, citations_g = await verify_claims(
        claims,
        items,
        chunk_lookup=_UnhydratedLookup(),
        allowed_workspace_ids={"ws-a"},
        claim_grounder=ExtractiveGrounder(),
    )
    assert verified_g == []
    assert citations_g == []
