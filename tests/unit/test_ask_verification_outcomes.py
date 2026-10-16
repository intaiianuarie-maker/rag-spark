"""Unit tests for T-OR-12: API/UI citation and verification outcomes (Stages 11 & 12 visible).

Verifies that:
- Stage 11 deterministic citation integrity outcomes are visible on Answer.
- Stage 12 Gemma semantic verification reports and claim verdicts are visible on Answer.
- Publication gate enforces fail-closed publication decisions:
  - Supported claims publish as "verified"
  - Contradicted claims abstain with "CONTRADICTION_FOUND"
  - Uncertain verdicts/outages downgrade to "needs_review"
  - Structural integrity violations reject publication
- Sensitive/local-only workspaces never wire cloud semantic verifiers.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from answer.adapters.gemma_semantic_verifier import DEFAULT_GEMMA_MODEL
from answer.engine import AnswerEngine
from answer.models import Answer, AnswerStatus
from answer.semantic_verification import (
    CanonicalEvidence,
    ClaimVerdict,
    SemanticClaimVerifier,
    SupportSpan,
    Verdict,
)
from core.auth import ACLScope
from harness_fakes import make_item
from models.interfaces import GenerationRequest, GenerationResult, LLMProvider
from retrieval.interfaces import RetrievalOrchestrator

pytestmark = pytest.mark.unit


class _FakeOrchestrator(RetrievalOrchestrator):
    def __init__(self, items):
        self.items = items
        self.calls = 0

    async def retrieve(self, **kwargs):
        self.calls += 1
        return self.items


class _FakeLLM(LLMProvider):
    provider_name = "fake-answer"

    def __init__(self, text: str):
        self.text = text

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        return GenerationResult(text=self.text, model="test-gemma")

    def stream(self, request: GenerationRequest):
        raise NotImplementedError


class _MockSemanticVerifier(SemanticClaimVerifier):
    def __init__(self, verdict: Verdict = Verdict.SUPPORTED, reason_code: str = "DIRECT_SUPPORT"):
        self.verdict = verdict
        self.reason_code = reason_code
        self.calls = 0
        self._model = DEFAULT_GEMMA_MODEL

    async def verify_claim(
        self,
        claim_text: str,
        evidence: list[CanonicalEvidence],
        *,
        recheck: bool = False,
    ) -> ClaimVerdict:
        self.calls += 1
        return ClaimVerdict(
            claim_id=claim_text[:10],
            verdict=self.verdict,
            evidence_ids=[e.evidence_id for e in evidence],
            support_spans=[
                SupportSpan(evidence_id=e.evidence_id, start=0, end=len(e.content))
                for e in evidence
            ],
            contradictions=["Conflict"] if self.verdict == Verdict.CONTRADICTED else [],
            reason_code=self.reason_code,
        )


def _make_engine(
    *,
    llm_output: str,
    items: list | None = None,
    semantic_verifier: SemanticClaimVerifier | None = None,
) -> AnswerEngine:
    if items is None:
        items = [
            make_item(
                "Company revenue grew 20% in 2025.",
                0.95,
                workspace_id="ws-1",
                document_id="doc-1",
                chunk_id="chunk-1",
                page=5,
                section="Financials",
            )
        ]
    return AnswerEngine(
        orchestrator=_FakeOrchestrator(items),
        llm=_FakeLLM(llm_output),
        model="google/gemma-4-26b-a4b-it:free",
        semantic_verifier=semantic_verifier,
    )


@pytest.mark.asyncio
async def test_answer_model_has_verification_fields():
    ans = Answer(
        answer="Grounded answer text.",
        status=AnswerStatus.ANSWERED,
        citation_integrity={"valid": True},
        semantic_verification={"all_supported": True},
        publication_gate={"can_publish": True, "status": "verified"},
        publication_status="verified",
        verifier_model="google/gemma-4-26b-a4b-it:free",
    )
    dump = ans.model_dump()
    assert dump["citation_integrity"] == {"valid": True}
    assert dump["semantic_verification"] == {"all_supported": True}
    assert dump["publication_gate"] == {"can_publish": True, "status": "verified"}
    assert dump["publication_status"] == "verified"
    assert dump["verifier_model"] == "google/gemma-4-26b-a4b-it:free"


@pytest.mark.asyncio
async def test_engine_stage11_integrity_outcome_visible():
    llm_payload = json.dumps({
        "answer": "Company revenue grew 20% in 2025.",
        "claims": [{"text": "Company revenue grew 20% in 2025.", "chunk_ids": ["chunk-1"]}],
    })
    engine = _make_engine(llm_output=llm_payload)
    ans = await engine.answer(query="Revenue?", acl_scope=ACLScope(workspace_ids=["ws-1"]))

    assert ans.status == AnswerStatus.ANSWERED
    assert ans.citation_integrity is not None
    assert ans.citation_integrity["valid"] is True
    assert len(ans.citation_integrity["accepted_citations"]) == 1
    assert ans.citation_integrity["accepted_citations"][0]["chunk_id"] == "chunk-1"
    assert ans.citation_integrity["rejected_citations"] == []


@pytest.mark.asyncio
async def test_engine_stage11_rejection_recorded_on_answer():
    llm_payload = json.dumps({
        "answer": "Fabricated claim citing absent chunk.",
        "claims": [{
            "text": "Fabricated claim citing absent chunk.",
            "chunk_ids": ["non-existent-chunk"],
        }],
    })
    engine = _make_engine(llm_output=llm_payload)
    ans = await engine.answer(query="Query?", acl_scope=ACLScope(workspace_ids=["ws-1"]))

    assert ans.status == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert ans.citation_integrity is not None
    assert ans.citation_integrity["valid"] is False
    assert len(ans.citation_integrity["rejected_citations"]) == 1
    rej_chunk_id = ans.citation_integrity["rejected_citations"][0]["citation"]["chunk_id"]
    assert rej_chunk_id == "non-existent-chunk"


@pytest.mark.asyncio
async def test_engine_stage12_and_publication_gate_verified():
    llm_payload = json.dumps({
        "answer": "Company revenue grew 20% in 2025.",
        "claims": [{"text": "Company revenue grew 20% in 2025.", "chunk_ids": ["chunk-1"]}],
    })
    verifier = _MockSemanticVerifier(verdict=Verdict.SUPPORTED, reason_code="DIRECT_SUPPORT")
    engine = _make_engine(llm_output=llm_payload, semantic_verifier=verifier)

    ans = await engine.answer(query="Revenue?", acl_scope=ACLScope(workspace_ids=["ws-1"]))

    assert ans.status == AnswerStatus.ANSWERED
    assert ans.publication_status == "verified"
    assert ans.publication_gate is not None
    assert ans.publication_gate["can_publish"] is True
    assert ans.publication_gate["status"] == "verified"
    assert ans.semantic_verification is not None
    assert ans.semantic_verification["all_supported"] is True
    assert ans.verifier_model == DEFAULT_GEMMA_MODEL


@pytest.mark.asyncio
async def test_engine_stage12_contradicted_abstains_with_fail_closed():
    llm_payload = json.dumps({
        "answer": "Company revenue fell 50% in 2025.",
        "claims": [{"text": "Company revenue fell 50% in 2025.", "chunk_ids": ["chunk-1"]}],
    })
    verifier = _MockSemanticVerifier(
        verdict=Verdict.CONTRADICTED,
        reason_code="CONTRADICTION_FOUND",
    )
    engine = _make_engine(llm_output=llm_payload, semantic_verifier=verifier)

    ans = await engine.answer(query="Revenue drop?", acl_scope=ACLScope(workspace_ids=["ws-1"]))

    assert ans.status == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert ans.reason_code == "CONTRADICTION_FOUND"
    assert ans.publication_status == "abstain"
    assert ans.publication_gate is not None
    assert ans.publication_gate["can_publish"] is False
    assert ans.publication_gate["status"] == "abstain"


@pytest.mark.asyncio
async def test_engine_stage12_uncertain_downgrades_to_needs_review():
    llm_payload = json.dumps({
        "answer": "Company revenue grew 20% in 2025.",
        "claims": [{"text": "Company revenue grew 20% in 2025.", "chunk_ids": ["chunk-1"]}],
    })
    verifier = _MockSemanticVerifier(verdict=Verdict.UNCERTAIN, reason_code="VERIFIER_OUTAGE")
    engine = _make_engine(llm_output=llm_payload, semantic_verifier=verifier)

    ans = await engine.answer(query="Revenue?", acl_scope=ACLScope(workspace_ids=["ws-1"]))

    assert ans.status == AnswerStatus.NEEDS_REVIEW
    assert ans.publication_status == "needs_review"
    assert ans.publication_gate is not None
    assert ans.publication_gate["can_publish"] is False
    assert ans.publication_gate["status"] == "needs_review"


@pytest.mark.asyncio
async def test_engine_stage11_failure_causes_gate_rejection():
    llm_payload = json.dumps({
        "answer": "Claim with bad chunk.",
        "claims": [{"text": "Claim with bad chunk.", "chunk_ids": ["bad-chunk"]}],
    })
    verifier = _MockSemanticVerifier(verdict=Verdict.SUPPORTED)
    engine = _make_engine(llm_output=llm_payload, semantic_verifier=verifier)

    ans = await engine.answer(query="Query?", acl_scope=ACLScope(workspace_ids=["ws-1"]))

    # When all claims lack valid citations, CITATION_INVALID or REJECTED occurs
    assert ans.status == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert ans.citation_integrity["valid"] is False


@pytest.mark.asyncio
async def test_ask_router_egress_sensitive_guard(monkeypatch):
    import uuid

    from apps.api.app.ask.router import AskRequest, ask

    ws_id = uuid.uuid4()
    # Mock workspace config as sensitive
    mock_ws = MagicMock()
    mock_ws.sensitive = True
    mock_ws.routing_rules = ()
    monkeypatch.setattr("apps.api.app.ask.router.load_workspace_config", lambda wid: mock_ws)

    # Mock principal with permissions
    principal = MagicMock()
    principal.user_id = "user-1"
    monkeypatch.setattr("apps.api.app.ask.router.require", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "apps.api.app.ask.router.resolve_model",
        lambda wid, **kwargs: ("local-qwen", "sensitive_rule"),
    )

    # Mock engine
    mock_engine = MagicMock()
    mock_engine.semantic_verifier = None
    mock_engine.answer = AsyncMock(
        return_value=Answer(answer="Local safe answer", status=AnswerStatus.ANSWERED)
    )
    monkeypatch.setattr(
        "apps.api.app.ask.router.build_answer_engine",
        lambda **kwargs: mock_engine,
    )

    await ask(workspace_id=ws_id, body=AskRequest(query="Local question"), principal=principal)

    # Sensitive workspace must NEVER have attached cloud Gemma verifier
    assert mock_engine.semantic_verifier is None
