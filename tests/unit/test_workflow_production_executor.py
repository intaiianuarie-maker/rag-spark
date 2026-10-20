"""Unit tests for T-OR-15: Production Workflow Execution with Model & Verification Nodes."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from answer.citation_integrity import CandidateCitation
from answer.models import CanonicalChunk, Claim, DocumentStatus
from models.interfaces import GenerationResult
from workflow_engine import (
    NodeSpec,
    NodeType,
    PortSpec,
)
from workflow_engine.manifest import CapabilityNotSupportedError
from workflow_engine.production_executor import ProductionNodeExecutor

pytestmark = [pytest.mark.unit]


def _make_node(
    node_id: str,
    node_type: NodeType,
    config: dict | None = None,
    caps: list[str] | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        type=node_type,
        config=config or {},
        required_capabilities=caps or [],
        input_ports=[PortSpec(name="input", type="any")],
        output_ports=[PortSpec(name="output", type="any")],
    )


@pytest.mark.asyncio
async def test_production_executor_executes_gemma_llm_node() -> None:
    mock_provider = AsyncMock()
    mock_provider.generate.return_value = GenerationResult(
        text="Gemma workflow output response.",
        model="google/gemma-4-26b-a4b-it:free",
    )

    executor = ProductionNodeExecutor(llm_provider=mock_provider)
    node = _make_node(
        "llm-1",
        NodeType.LLM,
        config={
            "model": "google/gemma-4-26b-a4b-it:free",
            "system_prompt": "You are a test assistant.",
        },
        caps=["llm"],
    )

    result = await executor.execute_node(node, {"text": "Process this data"}, "ws-test")
    assert result["output"] == "Gemma workflow output response."
    assert mock_provider.generate.called
    req = mock_provider.generate.call_args[0][0]
    assert req.model == "google/gemma-4-26b-a4b-it:free"
    assert req.messages[1].content == "Process this data"


@pytest.mark.asyncio
async def test_production_executor_executes_gemma_paid_fallback_model() -> None:
    mock_provider = AsyncMock()
    mock_provider.generate.return_value = GenerationResult(
        text="Paid Gemma response.",
        model="google/gemma-4-26b-a4b-it",
    )

    executor = ProductionNodeExecutor(llm_provider=mock_provider)
    node = _make_node(
        "llm-paid",
        NodeType.LLM,
        config={"model": "google/gemma-4-26b-a4b-it"},
        caps=["llm"],
    )

    result = await executor.execute_node(node, "summarize", "ws-test")
    assert result["output"] == "Paid Gemma response."


@pytest.mark.asyncio
async def test_production_executor_refuses_unadmitted_model() -> None:
    mock_provider = AsyncMock()
    executor = ProductionNodeExecutor(llm_provider=mock_provider)
    node = _make_node(
        "llm-unadmitted",
        NodeType.LLM,
        config={"model": "unapproved/forbidden-model-xyz"},
        caps=["llm"],
    )

    with pytest.raises(CapabilityNotSupportedError, match="not admitted for workflow role"):
        await executor.execute_node(node, "prompt", "ws-test")
    assert not mock_provider.generate.called


@pytest.mark.asyncio
async def test_production_executor_verification_no_evidence_returns_unverified() -> None:
    """Node with requires_verification=True but NO evidence → verified=False."""
    mock_provider = AsyncMock()
    mock_provider.generate.return_value = GenerationResult(
        text="Verified evidence analysis.",
        model="google/gemma-4-26b-a4b-it:free",
    )

    executor = ProductionNodeExecutor(llm_provider=mock_provider)
    node = _make_node(
        "llm-verified",
        NodeType.LLM,
        config={"model": "google/gemma-4-26b-a4b-it:free", "requires_verification": True},
        caps=["llm"],
    )

    result = await executor.execute_node(node, "check facts", "ws-test")
    assert result["output"] == "Verified evidence analysis."
    assert result["verified"] is False
    assert result["verification_stage"] == "insufficient_evidence"


@pytest.mark.asyncio
async def test_production_executor_verification_invalid_citations() -> None:
    """Node with invalid/unresolvable citations → verified=False."""
    mock_provider = AsyncMock()
    mock_provider.generate.return_value = GenerationResult(
        text="The moon is made of cheese.",
        model="google/gemma-4-26b-a4b-it:free",
    )

    executor = ProductionNodeExecutor(llm_provider=mock_provider)
    node = _make_node(
        "llm-verified",
        NodeType.LLM,
        config={
            "model": "google/gemma-4-26b-a4b-it:free",
            "requires_verification": True,
            "evidence": {
                "claims": [Claim(text="The moon is made of cheese.", chunk_ids=["fake-chunk"])],
                "citations": [
                    CandidateCitation(
                        chunk_id="fake-chunk", claim_text="The moon is made of cheese."
                    )
                ],
                "canonical": {},
                "allowed_workspace_ids": {"ws-test"},
            },
        },
        caps=["llm"],
    )

    result = await executor.execute_node(node, "check facts", "ws-test")
    assert result["output"] == "The moon is made of cheese."
    assert result["verified"] is False
    assert result["verification_stage"] == "Stage 11 rejected"


@pytest.mark.asyncio
async def test_production_executor_verification_valid_claims_no_verifier() -> None:
    """Node with valid evidence passing Stage 11 (no Stage 12 verifier) → verified=True."""
    mock_provider = AsyncMock()
    mock_provider.generate.return_value = GenerationResult(
        text="The launch budget is 20 dollars.",
        model="google/gemma-4-26b-a4b-it:free",
    )

    canonical_content = "The team met to review expenses. The launch budget is 20 dollars."
    canonical = {
        "ch-1": CanonicalChunk(
            chunk_id="ch-1", workspace_id="ws-test", document_id="doc-1",
            filename="launch_plan.pdf", page=1, section="Budget",
            content=canonical_content, status=DocumentStatus.ACTIVE, metadata={},
        )
    }

    executor = ProductionNodeExecutor(llm_provider=mock_provider)
    node = _make_node(
        "llm-verified",
        NodeType.LLM,
        config={
            "model": "google/gemma-4-26b-a4b-it:free",
            "requires_verification": True,
            "evidence": {
                "claims": [Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-1"])],
                "citations": [
                    CandidateCitation(
                        chunk_id="ch-1", claim_text="The launch budget is 20 dollars."
                    )
                ],
                "canonical": canonical,
                "allowed_workspace_ids": {"ws-test"},
            },
        },
        caps=["llm"],
    )

    result = await executor.execute_node(node, "check facts", "ws-test")
    # T-RV19-02: no verifier => explicit unavailable, never a Stage 12 pass.
    assert result["verified"] is False
    assert result["verification_stage"] == "Stage 12 unavailable"


@pytest.mark.asyncio
async def test_production_executor_verification_valid_with_semantic_pass() -> None:
    """Node with valid evidence + Stage 12 verifier that agrees → verified=True."""
    from answer.semantic_verification import ClaimVerdict, Verdict

    class _PassVerifier:
        async def verify_claim(self, claim_text, evidence, *, recheck=False):
            return ClaimVerdict(
                claim_id=claim_text, verdict=Verdict.SUPPORTED,
                evidence_ids=[], reason_code="SUPPORTED",
            )

    mock_provider = AsyncMock()
    mock_provider.generate.return_value = GenerationResult(
        text="The launch budget is 20 dollars.",
        model="google/gemma-4-26b-a4b-it:free",
    )

    canonical_content = "The team met to review expenses. The launch budget is 20 dollars."
    canonical = {
        "ch-1": CanonicalChunk(
            chunk_id="ch-1", workspace_id="ws-test", document_id="doc-1",
            filename="launch_plan.pdf", page=1, section="Budget",
            content=canonical_content, status=DocumentStatus.ACTIVE, metadata={},
        )
    }

    executor = ProductionNodeExecutor(llm_provider=mock_provider)
    node = _make_node(
        "llm-verified",
        NodeType.LLM,
        config={
            "model": "google/gemma-4-26b-a4b-it:free",
            "requires_verification": True,
            "evidence": {
                "claims": [Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-1"])],
                "citations": [
                    CandidateCitation(
                        chunk_id="ch-1", claim_text="The launch budget is 20 dollars."
                    )
                ],
                "canonical": canonical,
                "allowed_workspace_ids": {"ws-test"},
                "verifier": _PassVerifier(),
            },
        },
        caps=["llm"],
    )

    result = await executor.execute_node(node, "check facts", "ws-test")
    assert result["verified"] is True
    assert result["verification_stage"] == "Stage 11 & 12"


@pytest.mark.asyncio
async def test_production_executor_verification_contradicted_by_semantic() -> None:
    """Node with valid Stage 11 evidence but Stage 12 contradiction → verified=False."""
    from answer.semantic_verification import ClaimVerdict, Verdict

    class _ContradictVerifier:
        async def verify_claim(self, claim_text, evidence, *, recheck=False):
            return ClaimVerdict(
                claim_id=claim_text, verdict=Verdict.CONTRADICTED,
                evidence_ids=[], reason_code="CONTRADICTION_FOUND",
            )

    mock_provider = AsyncMock()
    mock_provider.generate.return_value = GenerationResult(
        text="The launch budget is 20 dollars.",
        model="google/gemma-4-26b-a4b-it:free",
    )

    canonical_content = "The team met to review expenses. The launch budget is 20 dollars."
    canonical = {
        "ch-1": CanonicalChunk(
            chunk_id="ch-1", workspace_id="ws-test", document_id="doc-1",
            filename="launch_plan.pdf", page=1, section="Budget",
            content=canonical_content, status=DocumentStatus.ACTIVE, metadata={},
        )
    }

    executor = ProductionNodeExecutor(llm_provider=mock_provider)
    node = _make_node(
        "llm-verified",
        NodeType.LLM,
        config={
            "model": "google/gemma-4-26b-a4b-it:free",
            "requires_verification": True,
            "evidence": {
                "claims": [Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-1"])],
                "citations": [
                    CandidateCitation(
                        chunk_id="ch-1", claim_text="The launch budget is 20 dollars."
                    )
                ],
                "canonical": canonical,
                "allowed_workspace_ids": {"ws-test"},
                "verifier": _ContradictVerifier(),
            },
        },
        caps=["llm"],
    )

    result = await executor.execute_node(node, "check facts", "ws-test")
    assert result["verified"] is False
    assert "abstain" in result["verification_stage"].lower()


@pytest.mark.asyncio
async def test_production_executor_deterministic_primitives() -> None:
    executor = ProductionNodeExecutor()

    # Trigger
    trig = _make_node("t1", NodeType.TRIGGER)
    assert await executor.execute_node(trig, {"k": "v"}, "ws-1") == {"output": {"k": "v"}}

    trans = _make_node("tr1", NodeType.TRANSFORM, config={"template": "Hello {name}!"})
    res_trans = await executor.execute_node(trans, {"name": "World"}, "ws-1")
    assert res_trans == {"output": "Hello World!"}

    # Condition
    cond = _make_node("c1", NodeType.CONDITION, config={"field": "status", "expected": "active"})
    assert await executor.execute_node(cond, {"status": "active"}, "ws-1") == {
        "result": True,
        "output": {"status": "active"},
    }

    # Output
    out = _make_node("o1", NodeType.OUTPUT)
    assert await executor.execute_node(out, "final value", "ws-1") == {"output": "final value"}


@pytest.mark.asyncio
async def test_production_executor_verification_recording_verifier_receives_output() -> None:
    """T-RV19-01: Stage 12 verifier must receive the generated output text."""
    from answer.semantic_verification import ClaimVerdict, Verdict

    class _RecordingVerifier:
        def __init__(self) -> None:
            self.seen: list[str] = []

        async def verify_claim(self, claim_text, evidence, *, recheck=False):
            self.seen.append(claim_text)
            return ClaimVerdict(
                claim_id=claim_text, verdict=Verdict.SUPPORTED,
                evidence_ids=[], reason_code="SUPPORTED",
            )

    recorder = _RecordingVerifier()
    mock_provider = AsyncMock()
    generated = "The launch budget is 20 dollars."
    mock_provider.generate.return_value = GenerationResult(
        text=generated,
        model="google/gemma-4-26b-a4b-it:free",
    )

    canonical_content = "The team met to review expenses. The launch budget is 20 dollars."
    canonical = {
        "ch-1": CanonicalChunk(
            chunk_id="ch-1", workspace_id="ws-test", document_id="doc-1",
            filename="launch_plan.pdf", page=1, section="Budget",
            content=canonical_content, status=DocumentStatus.ACTIVE, metadata={},
        )
    }

    executor = ProductionNodeExecutor(llm_provider=mock_provider)
    node = _make_node(
        "llm-verified",
        NodeType.LLM,
        config={
            "model": "google/gemma-4-26b-a4b-it:free",
            "requires_verification": True,
            "evidence": {
                "citations": [CandidateCitation(chunk_id="ch-1", claim_text=generated)],
                "canonical": canonical,
                "allowed_workspace_ids": {"ws-test"},
                "verifier": recorder,
            },
        },
        caps=["llm"],
    )

    result = await executor.execute_node(node, "check facts", "ws-test")
    assert result["output"] == generated
    assert result["verified"] is True
    assert recorder.seen == [generated], recorder.seen


@pytest.mark.asyncio
async def test_production_executor_verification_checks_output_not_evidence_claim() -> None:
    """T-RV19-01: the verified text is the generated output, not the evidence claim."""
    from answer.semantic_verification import ClaimVerdict, Verdict

    class _RecordingVerifier:
        def __init__(self) -> None:
            self.seen: list[str] = []

        async def verify_claim(self, claim_text, evidence, *, recheck=False):
            self.seen.append(claim_text)
            return ClaimVerdict(
                claim_id=claim_text, verdict=Verdict.SUPPORTED,
                evidence_ids=[], reason_code="SUPPORTED",
            )

    recorder = _RecordingVerifier()
    mock_provider = AsyncMock()
    generated = "The moon is made of cheese."
    mock_provider.generate.return_value = GenerationResult(
        text=generated,
        model="google/gemma-4-26b-a4b-it:free",
    )

    canonical = {
        "ch-1": CanonicalChunk(
            chunk_id="ch-1", workspace_id="ws-test", document_id="doc-1",
            filename="launch_plan.pdf", page=1, section="Budget",
            content="The launch budget is 20 dollars.",
            status=DocumentStatus.ACTIVE, metadata={},
        )
    }

    executor = ProductionNodeExecutor(llm_provider=mock_provider)
    node = _make_node(
        "llm-verified",
        NodeType.LLM,
        config={
            "model": "google/gemma-4-26b-a4b-it:free",
            "requires_verification": True,
            "evidence": {
                # The configured claim differs from what the model generated.
                "claims": [Claim(text="The launch budget is 20 dollars.", chunk_ids=["ch-1"])],
                "canonical": canonical,
                "allowed_workspace_ids": {"ws-test"},
                "verifier": recorder,
            },
        },
        caps=["llm"],
    )

    result = await executor.execute_node(node, "check facts", "ws-test")
    assert result["output"] == generated
    # The verifier must see the GENERATED text, never the preconfigured claim.
    assert recorder.seen == [generated], recorder.seen
    assert "The launch budget is 20 dollars." not in recorder.seen


@pytest.mark.asyncio
async def test_production_executor_verification_uses_runtime_verifier() -> None:
    """T-RV19-02: the verifier can be resolved through the runtime DI boundary."""
    from answer.semantic_verification import ClaimVerdict, Verdict

    class _RuntimeVerifier:
        def __init__(self) -> None:
            self.seen: list[str] = []

        async def verify_claim(self, claim_text, evidence, *, recheck=False):
            self.seen.append(claim_text)
            return ClaimVerdict(
                claim_id=claim_text, verdict=Verdict.SUPPORTED,
                evidence_ids=[], reason_code="SUPPORTED",
            )

    runtime_verifier = _RuntimeVerifier()
    mock_provider = AsyncMock()
    generated = "The launch budget is 20 dollars."
    mock_provider.generate.return_value = GenerationResult(
        text=generated,
        model="google/gemma-4-26b-a4b-it:free",
    )

    canonical = {
        "ch-1": CanonicalChunk(
            chunk_id="ch-1", workspace_id="ws-test", document_id="doc-1",
            filename="launch_plan.pdf", page=1, section="Budget",
            content="The launch budget is 20 dollars.",
            status=DocumentStatus.ACTIVE, metadata={},
        )
    }

    # No verifier in the node evidence: it must come from the executor DI.
    executor = ProductionNodeExecutor(
        llm_provider=mock_provider, verifier=runtime_verifier
    )
    node = _make_node(
        "llm-verified",
        NodeType.LLM,
        config={
            "model": "google/gemma-4-26b-a4b-it:free",
            "requires_verification": True,
            "evidence": {
                "claims": [Claim(text=generated, chunk_ids=["ch-1"])],
                "canonical": canonical,
                "allowed_workspace_ids": {"ws-test"},
            },
        },
        caps=["llm"],
    )

    result = await executor.execute_node(node, "check facts", "ws-test")
    assert result["verified"] is True
    assert result["verification_stage"] == "Stage 11 & 12"
    assert runtime_verifier.seen == [generated], runtime_verifier.seen
