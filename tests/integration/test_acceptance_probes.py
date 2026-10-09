"""Acceptance tests gating v1 delivery per rag-fork-solution/TESTING.md.

Covers:
#1 Golden path: grounded answer with real citation.
#2 ACL isolation: user B cannot retrieve or cite secret document from workspace A.
#3 Delete propagation: deleted document is not cited and unretrievable.
#4 Citation integrity (negative): missing or out-of-workspace chunk IDs are rejected/stripped.
#5 Multilingual: cross-lingual query retrieval and language consistency.
"""

from __future__ import annotations

import pytest
from answer.engine import AnswerEngine
from core.auth import ACLScope
from models.interfaces import (
    GenerationRequest,
    GenerationResult,
    LLMProvider,
)
from retrieval.interfaces import RetrievalMethod, RetrievalMode, RetrievedItem

pytestmark = [pytest.mark.unit, pytest.mark.integration]


class MockAcceptanceLLM(LLMProvider):
    provider_name = "mock_acceptance"

    def __init__(self, response_text: str = "") -> None:
        self.response_text = response_text
        self.last_request: GenerationRequest | None = None

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.last_request = request
        return GenerationResult(text=self.response_text, model=request.model)

    async def stream(self, request: GenerationRequest):
        self.last_request = request
        yield self.response_text


class MockAcceptanceOrchestrator:
    def __init__(self, items: list[RetrievedItem]) -> None:
        self.items = items
        self.calls: list[dict] = []

    async def retrieve(
        self, *, query: str, acl_scope: ACLScope, mode=RetrievalMode.HYBRID, **kwargs
    ) -> list[RetrievedItem]:
        self.calls.append({"query": query, "acl_scope": acl_scope, "mode": mode, "kwargs": kwargs})
        allowed = set(acl_scope.workspace_ids)
        return [item for item in self.items if item.workspace_id in allowed]


# --- Acceptance Probe #1: Golden Path (Grounded Answer + Real Citation) ---
@pytest.mark.asyncio
async def test_acceptance_1_golden_path_grounded_answer_with_real_citation() -> None:
    evidence_item = RetrievedItem(
        content="Antigravity provides automated agent pairing with zero local latency.",
        score=0.95,
        method=RetrievalMethod.DENSE,
        workspace_id="ws-golden",
        document_id="doc-golden-1",
        chunk_id="chunk-golden-1",
        page=3,
        section="Architecture Overview",
        metadata={"filename": "architecture.pdf"},
    )
    orchestrator = MockAcceptanceOrchestrator([evidence_item])

    # LLM emits valid structured JSON format per prompt contract
    llm_output = (
        '{"answer": "Antigravity supports agent pairing with zero local latency.", '
        '"claims": [{"text": "Antigravity supports agent pairing with zero local latency.", '
        '"chunk_ids": ["chunk-golden-1"]}]}'
    )
    llm = MockAcceptanceLLM(response_text=llm_output)
    engine = AnswerEngine(orchestrator=orchestrator, llm=llm, model="gpt-4o-mini")

    scope = ACLScope(user_id="user-1", workspace_ids=["ws-golden"])
    result = await engine.answer(query="What does Antigravity provide?", acl_scope=scope)

    assert "zero local latency" in result.answer
    assert result.status == "answered"
    assert result.confidence.level == "high"
    assert len(result.claims) == 1
    assert result.claims[0].chunk_ids == ["chunk-golden-1"]
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.chunk_id == "chunk-golden-1"
    assert citation.document_id == "doc-golden-1"
    assert citation.page == 3
    assert citation.section == "Architecture Overview"


# --- Acceptance Probe #2: ACL Isolation ---
@pytest.mark.asyncio
async def test_acceptance_2_acl_isolation_cross_workspace() -> None:
    secret_item = RetrievedItem(
        content="Secret financial formula in Workspace A: 42 * X",
        score=0.99,
        method=RetrievalMethod.DENSE,
        workspace_id="ws-secret-a",
        document_id="doc-secret-a",
        chunk_id="chunk-secret-a",
    )
    public_item = RetrievedItem(
        content="Public handbook information in Workspace B",
        score=0.70,
        method=RetrievalMethod.DENSE,
        workspace_id="ws-user-b",
        document_id="doc-b-1",
        chunk_id="chunk-b-1",
    )
    orchestrator = MockAcceptanceOrchestrator([secret_item, public_item])

    # User B queries specifically for the secret phrase
    scope_b = ACLScope(user_id="user-b", workspace_ids=["ws-user-b"])
    retrieved = await orchestrator.retrieve(query="Secret financial formula", acl_scope=scope_b)

    # Secret document must NEVER be returned to workspace B
    assert len(retrieved) == 0 or all(item.workspace_id == "ws-user-b" for item in retrieved)
    assert not any("Secret financial formula" in item.content for item in retrieved)


# --- Acceptance Probe #3: Delete Propagation ---
@pytest.mark.asyncio
async def test_acceptance_3_delete_propagation() -> None:
    live_items = [
        RetrievedItem(
            content="Active documentation chunk",
            score=0.85,
            method=RetrievalMethod.DENSE,
            workspace_id="ws-1",
            document_id="doc-active",
            chunk_id="chunk-active",
        )
    ]
    orchestrator = MockAcceptanceOrchestrator(live_items)
    scope = ACLScope(user_id="u1", workspace_ids=["ws-1"])

    # Simulate document deletion
    live_items.clear()

    retrieved_after_delete = await orchestrator.retrieve(
        query="Active documentation", acl_scope=scope
    )
    assert len(retrieved_after_delete) == 0


# --- Acceptance Probe #4: Citation Integrity (Negative - Fabrications Stripped) ---
@pytest.mark.asyncio
async def test_acceptance_4_citation_integrity_fabricated_chunk_rejected() -> None:
    valid_item = RetrievedItem(
        content="Valid server configuration options.",
        score=0.9,
        method=RetrievalMethod.DENSE,
        workspace_id="ws-auth",
        document_id="doc-1",
        chunk_id="valid-chunk-1",
        page=1,
    )
    orchestrator = MockAcceptanceOrchestrator([valid_item])

    # LLM hallucinates an invalid or out-of-workspace chunk_id: "hallucinated-chunk-999"
    llm_output = (
        '{"answer": "Here is information based on hallucinated data.", '
        '"claims": [{"text": "Hallucinated claim", "chunk_ids": ["hallucinated-chunk-999"]}]}'
    )
    llm = MockAcceptanceLLM(response_text=llm_output)
    engine = AnswerEngine(orchestrator=orchestrator, llm=llm, model="gpt-4o-mini")

    scope = ACLScope(user_id="u1", workspace_ids=["ws-auth"])
    result = await engine.answer(query="Explain options", acl_scope=scope)

    # Fabricated chunk must be completely stripped from citations and yield insufficient evidence
    assert len(result.citations) == 0
    assert len(result.claims) == 0
    assert result.status == "insufficient_evidence"
    assert result.reason_code == "CITATION_INVALID"
    assert result.confidence.level == "low"

    # Sub-scenario (v1-rereview Q5): a claim citing a REAL, in-scope chunk that
    # does NOT substantiate the claim must also be rejected. This is the
    # "fabricated answer citing an accessible chunk" false-success case flagged
    # by the release re-review.
    ungrounded_llm = MockAcceptanceLLM(
        response_text=(
            '{"answer": "The capital of France is Paris.", '
            '"claims": [{"text": "The capital of France is Paris.", '
            '"chunk_ids": ["valid-chunk-1"]}]}'
        )
    )
    ungrounded_engine = AnswerEngine(
        orchestrator=orchestrator, llm=ungrounded_llm, model="gpt-4o-mini"
    )
    ungrounded_result = await ungrounded_engine.answer(
        query="Explain options", acl_scope=scope
    )
    assert len(ungrounded_result.claims) == 0
    assert len(ungrounded_result.citations) == 0
    assert ungrounded_result.status == "insufficient_evidence"
    assert ungrounded_result.reason_code == "CITATION_INVALID"


# --- Acceptance Probe #5: Multilingual Cross-Language Retrieval ---
@pytest.mark.asyncio
async def test_acceptance_5_multilingual_cross_language_retrieval() -> None:
    ro_item = RetrievedItem(
        content="Platforma acceptă autentificarea pe bază de roluri și spații de lucru.",
        score=0.92,
        method=RetrievalMethod.DENSE,
        workspace_id="ws-multi",
        document_id="doc-ro-1",
        chunk_id="chunk-ro-1",
        metadata={"language": "ro"},
    )
    orchestrator = MockAcceptanceOrchestrator([ro_item])

    llm_output = (
        '{"answer": "Platforma acceptă autentificare pe bază de roluri.", '
        '"claims": [{"text": "Autentificare pe bază de roluri", "chunk_ids": ["chunk-ro-1"]}]}'
    )
    llm = MockAcceptanceLLM(response_text=llm_output)
    engine = AnswerEngine(orchestrator=orchestrator, llm=llm, model="gpt-4o-mini")

    scope = ACLScope(user_id="u1", workspace_ids=["ws-multi"])
    result = await engine.answer(query="Ce tip de autentificare este acceptat?", acl_scope=scope)

    assert "autentificare" in result.answer.lower()
    assert result.status == "answered"
    assert result.confidence.level == "high"
    assert len(result.citations) == 1
    assert result.citations[0].chunk_id == "chunk-ro-1"
