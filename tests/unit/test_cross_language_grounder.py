"""Unit tests for conservative cross-language verification path (T-R09).

Proves:
1. Strict extractive verifier (ExtractiveGrounder) is untouched:
   - threshold is 1.0 (never lowered);
   - still abstains on cross-language translations.
2. Conservative cross-language verification path (CrossLanguageGrounder):
   - threshold is 1.0 (never lowered);
   - RO/PT correct translations of EN sources verify (and vice versa);
   - original-source spans are returned pointing directly to the source sentence in chunk;
   - wrong-language answers (DE, FR, ES, IT) abstain;
   - wrong facts/entities/quantities/role-swaps abstain;
   - negation, conditionals, approximation hedges, and reported attribution abstain;
   - AnswerEngine e2e verifies correct translation with HIGH confidence and
     abstains on wrong answers.
"""

from __future__ import annotations

import json

import pytest
from core.auth import ACLScope
from models.interfaces import GenerationRequest, GenerationResult, LLMProvider
from retrieval.interfaces import RetrievalMethod, RetrievedItem

from packages.answer import AnswerEngine
from packages.answer.engine import AnswerStatus, ConfidenceLevel
from packages.answer.models import Claim
from packages.answer.verification import (
    CrossLanguageGrounder,
    ExtractiveGrounder,
    verify_claims,
)


class _FakeLLM(LLMProvider):
    """Hermetic LLM test double returning fixed output."""

    provider_name = "fake-cross-lang"

    def __init__(self, response_text: str) -> None:
        self.text = response_text

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        return GenerationResult(text=self.text, model=request.model)

    def stream(self, request: GenerationRequest):
        raise NotImplementedError


class _FakeOrchestrator:
    """Hermetic orchestrator double returning fixed items."""

    def __init__(self, items: list[RetrievedItem]) -> None:
        self._items = list(items)

    async def retrieve(self, **kwargs) -> list[RetrievedItem]:
        return list(self._items)


def _make_item(
    content: str, chunk_id: str = "ch-1", workspace_id: str = "ws-test"
) -> RetrievedItem:
    return RetrievedItem(
        content=content,
        score=0.95,
        method=RetrievalMethod.DENSE,
        workspace_id=workspace_id,
        document_id="doc-1",
        chunk_id=chunk_id,
        page=1,
    )


# ---------------------------------------------------------------------------
# 1. Extractive verifier untouched & thresholds unchanged
# ---------------------------------------------------------------------------

def test_extractive_grounder_untouched_and_thresholds_unchanged() -> None:
    """The strict extractive verifier remains untouched: threshold 1.0, and it still
    abstains on cross-language translations."""
    extractive = ExtractiveGrounder()
    cross_lang = CrossLanguageGrounder()

    assert extractive.threshold == 1.0
    assert cross_lang.threshold == 1.0

    # ExtractiveGrounder still rejects cross-language translation
    assert (
        extractive.score("Capitala Franței este Paris.", "The capital of France is Paris.")
        == 0.0
    )
    assert (
        extractive.locate("Capitala Franței este Paris.", "The capital of France is Paris.")
        is None
    )


# ---------------------------------------------------------------------------
# 2. RO and PT correct translations verify with original-source spans
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("source", "claim", "expected_span_text"),
    [
        (
            "The capital of France is Paris.",
            "Capitala Franței este Paris.",
            "The capital of France is Paris",
        ),
        (
            "The capital of France is Paris.",
            "A capital da França é Paris.",
            "The capital of France is Paris",
        ),
        (
            "The budget is 900 dollars.",
            "Bugetul este de 900 de dolari.",
            "The budget is 900 dollars",
        ),
        (
            "The budget is 900 dollars.",
            "O orçamento é de 900 dólares.",
            "The budget is 900 dollars",
        ),
        (
            "The platform supports role-based authentication and workspaces.",
            "Platforma acceptă autentificarea pe bază de roluri și spații de lucru.",
            "The platform supports role-based authentication and workspaces",
        ),
        (
            "The platform supports role-based authentication and workspaces.",
            "A plataforma suporta autenticação baseada em funções e espaços de trabalho.",
            "The platform supports role-based authentication and workspaces",
        ),
        # Reverse translation direction: RO -> EN and PT -> EN
        (
            "Bugetul este de 900 de dolari.",
            "The budget is 900 dollars.",
            "Bugetul este de 900 de dolari",
        ),
        (
            "O orçamento é de 900 dólares.",
            "The budget is 900 dollars.",
            "O orçamento é de 900 dólares",
        ),
        # RO -> PT and PT -> RO
        (
            "Bugetul este de 900 de dolari.",
            "O orçamento é de 900 dólares.",
            "Bugetul este de 900 de dolari",
        ),
        (
            "O orçamento é de 900 dólares.",
            "Bugetul este de 900 de dolari.",
            "O orçamento é de 900 dólares",
        ),
    ],
)
def test_cross_language_translations_verify(
    source: str, claim: str, expected_span_text: str
) -> None:
    """Proves that correct RO and PT translations verify with score 1.0 and original-source span."""
    grounder = CrossLanguageGrounder()
    assert grounder.score(claim, source) == 1.0

    span = grounder.locate(claim, source)
    assert span is not None
    start, end = span
    assert source[start:end].rstrip(".,;!?") == expected_span_text


def test_cross_language_span_in_multi_sentence_chunk() -> None:
    """Original-source span locates the exact asserting sentence in a multi-sentence chunk."""
    chunk = "Alice met Acme. The budget is 900 dollars. Bob called Charlie."
    grounder = CrossLanguageGrounder()

    span = grounder.locate("Bugetul este de 900 de dolari.", chunk)
    assert span is not None
    start, end = span
    assert chunk[start:end] == "The budget is 900 dollars"


# ---------------------------------------------------------------------------
# 3. Wrong-language answers abstain (abstention-first)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "wrong_lang_claim",
    [
        "Das Budget beträgt 900 Dollar.",  # German
        "Le budget est de 900 dollars.",  # French
        "El presupuesto es de 900 dólares.",  # Spanish
        "Il budget è di 900 dollari.",  # Italian
    ],
)
def test_wrong_language_answers_abstain(wrong_lang_claim: str) -> None:
    """Unsupported language answers immediately abstain with score 0.0 and None span."""
    grounder = CrossLanguageGrounder()
    source = "The budget is 900 dollars."
    assert grounder.score(wrong_lang_claim, source) == 0.0
    assert grounder.locate(wrong_lang_claim, source) is None


# ---------------------------------------------------------------------------
# 4. Wrong facts / entities / numbers / role reversals abstain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("source", "claim"),
    [
        # Entity mismatch (capital of Romania is not Paris; source is France)
        ("The capital of France is Paris.", "Capitala României este Paris."),
        # City mismatch (Lyon is not Paris)
        ("The capital of France is Paris.", "Capitala Franței este Lyon."),
        # Quantity mismatch (20 vs 900)
        ("The budget is 900 dollars.", "Bugetul este de 20 de dolari."),
        ("The budget is 900 dollars.", "O orçamento é de 20 dólares."),
        # Role swap (Alice paid Bob != Bob paid Alice)
        ("Alice paid Bob.", "Bob a plătit lui Alice."),
        # Extra ungrounded claim clauses
        ("The budget is 900 dollars.", "Bugetul este de 900 de dolari și avem mașini noi."),
    ],
)
def test_cross_language_wrong_facts_abstain(source: str, claim: str) -> None:
    """Wrong entities, cities, quantities, role swaps, and extraneous content abstain."""
    grounder = CrossLanguageGrounder()
    assert grounder.score(claim, source) == 0.0
    assert grounder.locate(claim, source) is None


# ---------------------------------------------------------------------------
# 5. Negation, conditionals, approximations, and attribution frames abstain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("source", "claim"),
    [
        # Negation in source (source refutes budget 900)
        ("It is false that the budget is 900 dollars.", "Bugetul este de 900 de dolari."),
        ("Nu este adevărat că bugetul este de 900 de dolari.", "The budget is 900 dollars."),
        ("The budget is not 900 dollars.", "Bugetul este de 900 de dolari."),
        # Negation in claim (claim refutes fact asserted in source)
        ("The budget is 900 dollars.", "Nu este adevărat că bugetul este de 900 de dolari."),
        ("The capital of France is Paris.", "Capitala Franței nu este Paris."),
        # Conditionals in source
        ("If the budget is 900 dollars, call Alice.", "Bugetul este de 900 de dolari."),
        ("Dacă bugetul este de 900 de dolari, sună-o pe Alice.", "The budget is 900 dollars."),
        # Conditionals in claim
        ("The budget is 900 dollars.", "Dacă bugetul este de 900 de dolari..."),
        # Approximations/hedges in source
        ("The budget is about 900 dollars.", "Bugetul este de 900 de dolari."),
        ("O orçamento é cerca de 900 dólares.", "The budget is 900 dollars."),
        # Approximations/hedges in claim
        ("The budget is 900 dollars.", "Bugetul este aproximativ 900 de dolari."),
        # Indirect speech / attribution in source
        ("Alice reported that the budget is 900 dollars.", "Bugetul este de 900 de dolari."),
        ("Alice a declarat că bugetul este de 900 de dolari.", "The budget is 900 dollars."),
        ("Alice afirmou que o orçamento é de 900 dólares.", "The budget is 900 dollars."),
        # Quoted attribution in source
        ('"The budget is 900 dollars," said Alice.', "Bugetul este de 900 de dolari."),
    ],
)
def test_cross_language_commitment_guards_abstain(source: str, claim: str) -> None:
    """Non-asserting sentences (negation, conditionals, hedges, attribution) abstain."""
    grounder = CrossLanguageGrounder()
    assert grounder.score(claim, source) == 0.0
    assert grounder.locate(claim, source) is None


# ---------------------------------------------------------------------------
# 6. verify_claims integration with CrossLanguageGrounder
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_claims_accepts_valid_translation() -> None:
    """verify_claims retains valid translation with grounding_score 1.0 and original span."""
    source_chunk = "Background notes. The capital of France is Paris. Concluding remarks."
    item = _make_item(source_chunk, chunk_id="ch-paris")
    claim = Claim(text="Capitala Franței este Paris.", chunk_ids=["ch-paris"])

    verified, citations = await verify_claims(
        [claim],
        [item],
        allowed_workspace_ids={"ws-test"},
        claim_grounder=CrossLanguageGrounder(),
    )

    assert len(verified) == 1
    assert verified[0].text == "Capitala Franței este Paris."
    assert len(citations) == 1
    citation = citations[0]
    assert citation.grounding_score == 1.0
    assert citation.span is not None
    start, end = citation.span
    assert source_chunk[start:end] == "The capital of France is Paris"
    assert citation.content == source_chunk


@pytest.mark.asyncio
async def test_verify_claims_rejects_wrong_translation() -> None:
    """verify_claims drops citations and claims when translation is incorrect or wrong language."""
    item = _make_item("The capital of France is Paris.", chunk_id="ch-paris")
    bad_claims = [
        Claim(text="Capitala României este Paris.", chunk_ids=["ch-paris"]),
        Claim(text="Das Budget beträgt 900 Dollar.", chunk_ids=["ch-paris"]),
    ]

    verified, citations = await verify_claims(
        bad_claims,
        [item],
        allowed_workspace_ids={"ws-test"},
        claim_grounder=CrossLanguageGrounder(),
    )

    assert verified == []
    assert citations == []


# ---------------------------------------------------------------------------
# 7. AnswerEngine end-to-end with CrossLanguageGrounder
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_answer_engine_end_to_end_cross_language_success() -> None:
    """AnswerEngine produces ANSWERED status and HIGH confidence for verified RO answer."""
    source_content = "The capital of France is Paris."
    item = _make_item(source_content, chunk_id="ch-paris")
    orchestrator = _FakeOrchestrator([item])

    llm_output = json.dumps({
        "answer": "Capitala Franței este Paris.",
        "claims": [{"text": "Capitala Franței este Paris.", "chunk_ids": ["ch-paris"]}],
    })
    engine = AnswerEngine(
        orchestrator=orchestrator,
        llm=_FakeLLM(llm_output),
        model="test-model",
        claim_grounder=CrossLanguageGrounder(),
    )

    result = await engine.answer(
        query="Care este capitala Franței?",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-test"]),
    )

    assert result.status == AnswerStatus.ANSWERED
    assert result.confidence.level == ConfidenceLevel.HIGH
    assert len(result.citations) == 1
    assert result.citations[0].grounding_score == 1.0
    assert result.citations[0].span == (0, 30)
    assert source_content[0:30] == "The capital of France is Paris"
    assert "Capitala Franței este Paris." in result.answer


@pytest.mark.asyncio
async def test_answer_engine_end_to_end_cross_language_abstains_on_wrong_fact() -> None:
    """AnswerEngine abstains with INSUFFICIENT_EVIDENCE when translation fact is wrong."""
    source_content = "The capital of France is Paris."
    item = _make_item(source_content, chunk_id="ch-paris")
    orchestrator = _FakeOrchestrator([item])

    # Model hallucinates that Bucharest is Paris
    llm_output = json.dumps({
        "answer": "Capitala României este Paris.",
        "claims": [{"text": "Capitala României este Paris.", "chunk_ids": ["ch-paris"]}],
    })
    engine = AnswerEngine(
        orchestrator=orchestrator,
        llm=_FakeLLM(llm_output),
        model="test-model",
        claim_grounder=CrossLanguageGrounder(),
    )

    result = await engine.answer(
        query="Care este capitala?",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-test"]),
    )

    assert result.status == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code == "CITATION_INVALID"
    assert result.confidence.level == ConfidenceLevel.LOW
    assert len(result.claims) == 0
    assert len(result.citations) == 0
