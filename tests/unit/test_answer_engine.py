"""Unit tests for the answer engine + citation verification (TASK-M4-002).

Covers the ticket's custom glue: prompt construction, structured-output
parsing, citation verification (exists AND in-ACL, no fabrication), and the
engine pipeline incl. the unparseable-output fallback.
"""

from __future__ import annotations

import pytest
from answer.engine import AnswerEngine
from answer.models import Answer, Claim
from answer.prompts import build_prompt, parse_model_output
from answer.verification import verify_claims
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
        '"claims": [{"text": "ACL is enforced", "chunk_ids": ["c-1", "c-2"]}]}'
    )
    result = await _engine(llm).answer(
        query="what governs access?", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert isinstance(result, Answer)
    assert result.answer == "Permissions are enforced."
    # c-2 is out-of-scope -> stripped; claim survives with c-1 only
    assert result.claims[0].chunk_ids == ["c-1"]
    assert [c.chunk_id for c in result.citations] == ["c-1"]
    # the prompt the model saw carried the retrieved context
    user_message = llm.requests[0].messages[-1]
    assert "c-1" in user_message.content


async def test_engine_unparseable_output_falls_back() -> None:
    llm = _FakeLLM("I cannot answer that.")
    result = await _engine(llm).answer(
        query="q", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert result.answer == "I cannot answer that."
    assert result.claims == [] and result.citations == []


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
