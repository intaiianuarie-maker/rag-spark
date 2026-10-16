"""Stage 12 — Gemma semantic citation verifier & publication gate tests (T-OR-06).

Hermetic, offline unit tests using mock LLM providers and chunk lookups.
Covers:
- Supported claims in English and Romanian (with diacritics).
- Partially supported claims with bounded recheck (moves to supported or stays partial).
- Unsupported claims (evidence mismatch, missing Stage 11 citations).
- Contradicted claims (negation, entities, numbers, dates).
- Fail-closed error handling (429 exhaustion, timeouts, refusals, malformed JSON, crashes).
- Local validation of evidence IDs and span bounds.
- Publication gate: verified pass, Stage 11 rejection, Stage 12 abstention, verifier outage,
  and concurrent lifecycle mutation/deletion.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from answer.adapters.gemma_semantic_verifier import (
    GemmaSemanticVerifier,
    _429Error,
    _ModelRefusedError,
)
from answer.citation_integrity import (
    CandidateCitation,
    CitationIntegrityReport,
    RejectedCitation,
    RejectionReason,
)
from answer.models import Citation, Claim
from answer.semantic_verification import (
    REASON_MALFORMED_JSON,
    REASON_MODEL_REFUSED,
    REASON_NO_EVIDENCE,
    REASON_SUPPORTED,
    REASON_TIMEOUT,
    REASON_UPSTREAM_429_EXHAUSTED,
    REASON_VERIFIER_OUTAGE,
    CanonicalEvidence,
    ClaimVerdict,
    PublicationStatus,
    SemanticVerificationReport,
    Verdict,
    evaluate_publication_gate,
    verify_semantic_claims,
)
from models.interfaces import GenerationRequest, GenerationResult, LLMProvider

pytestmark = pytest.mark.unit


class FakeLLMProvider(LLMProvider):
    """Controllable test double for LLM generation."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls.append(request)
        if not self.responses:
            raise RuntimeError("FakeLLMProvider exhausted responses")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return GenerationResult(
            text=resp,
            model=request.model,
            prompt_tokens=10,
            completion_tokens=20,
        )

    async def stream(self, request: GenerationRequest):
        res = await self.generate(request)
        yield res.text


class FakeChunkLookup:
    """Mock ChunkLookup for lifecycle revalidation."""

    def __init__(self, live_citations: dict[str, Citation]) -> None:
        self.live_citations = live_citations
        self.calls: list[list[str]] = []

    async def get(self, chunk_id: str) -> Citation | None:
        return self.live_citations.get(chunk_id)

    async def get_many(self, chunk_ids: Sequence[str]) -> dict[str, Citation]:
        self.calls.append(list(chunk_ids))
        return {cid: self.live_citations[cid] for cid in chunk_ids if cid in self.live_citations}


def _make_evidence(cid: str, content: str) -> CanonicalEvidence:
    return CanonicalEvidence(
        evidence_id=cid,
        content=content,
        filename="test.pdf",
        page=1,
    )


def _make_integrity_report(
    citations: list[Citation],
    rejected: list[RejectedCitation] | None = None,
) -> CitationIntegrityReport:
    rej = rejected or []
    return CitationIntegrityReport(
        valid=len(rej) == 0,
        accepted_citations=citations,
        rejected_citations=rej,
        omissions=[],
        unverified_claims=[],
    )


# ---------------------------------------------------------------------------
# Supported claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supported_claim_english() -> None:
    response_json = """{
      "verdict": "supported",
      "reason_code": "DIRECT_SUPPORT",
      "support_spans": [{"evidence_id": "ch-1", "start": 0, "end": 28}],
      "contradictions": []
    }"""
    provider = FakeLLMProvider([response_json])
    verifier = GemmaSemanticVerifier(provider)

    evidence = [_make_evidence("ch-1", "The company was founded in 2020 by Jane Doe.")]
    verdict = await verifier.verify_claim("Jane Doe founded the company in 2020.", evidence)

    assert verdict.verdict == Verdict.SUPPORTED
    assert verdict.reason_code == "DIRECT_SUPPORT"
    assert len(verdict.support_spans) == 1
    assert verdict.support_spans[0].evidence_id == "ch-1"
    assert verdict.support_spans[0].start == 0
    assert verdict.support_spans[0].end == 28


@pytest.mark.asyncio
async def test_supported_claim_romanian_diacritics() -> None:
    response_json = """{
      "verdict": "supported",
      "reason_code": "DIRECT_SUPPORT",
      "support_spans": [{"evidence_id": "ch-ro-1", "start": 0, "end": 45}],
      "contradictions": []
    }"""
    provider = FakeLLMProvider([response_json])
    verifier = GemmaSemanticVerifier(provider)

    evidence = [
        _make_evidence(
            "ch-ro-1",
            "Veniturile operaționale au crescut cu 15% în trimestrul al doilea.",
        )
    ]
    verdict = await verifier.verify_claim(
        "În T2 veniturile operaționale au înregistrat o creștere de 15%.",
        evidence,
    )

    assert verdict.verdict == Verdict.SUPPORTED
    assert len(verdict.support_spans) == 1


# ---------------------------------------------------------------------------
# Unsupported claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_claim_no_mention() -> None:
    response_json = """{
      "verdict": "unsupported",
      "reason_code": "NO_EVIDENCE",
      "support_spans": [],
      "contradictions": []
    }"""
    provider = FakeLLMProvider([response_json])
    verifier = GemmaSemanticVerifier(provider)

    evidence = [_make_evidence("ch-1", "Quarterly earnings grew 5% year over year.")]
    verdict = await verifier.verify_claim("The company announced a 3-for-1 stock split.", evidence)

    assert verdict.verdict == Verdict.UNSUPPORTED
    assert verdict.reason_code == "NO_EVIDENCE"


@pytest.mark.asyncio
async def test_unsupported_claim_missing_stage11_evidence() -> None:
    provider = FakeLLMProvider([])
    verifier = GemmaSemanticVerifier(provider)

    claim = Claim(text="Unbacked claim", chunk_ids=["missing-ch"])
    report = _make_integrity_report([])

    sem_report = await verify_semantic_claims([claim], report, verifier)

    assert not sem_report.all_supported
    assert sem_report.abstain_recommended
    assert len(sem_report.claim_verdicts) == 1
    assert sem_report.claim_verdicts[0].verdict == Verdict.UNSUPPORTED
    assert sem_report.claim_verdicts[0].reason_code == REASON_NO_EVIDENCE
    assert len(provider.calls) == 0  # Zero LLM calls when evidence did not survive Stage 11


# ---------------------------------------------------------------------------
# Contradicted claims (numbers, entities, negation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contradicted_claim_numbers_and_dates() -> None:
    response_json = """{
      "verdict": "contradicted",
      "reason_code": "CONTRADICTION_FOUND",
      "support_spans": [],
      "contradictions": ["total expenses reached 100,000 dollars, not 50,000"]
    }"""
    provider = FakeLLMProvider([response_json])
    verifier = GemmaSemanticVerifier(provider)

    evidence = [
        _make_evidence("ch-1", "Total expenses reached 100,000 dollars in fiscal year 2023.")
    ]
    verdict = await verifier.verify_claim(
        "Total expenses were 50,000 dollars in fiscal year 2023.",
        evidence,
    )

    assert verdict.verdict == Verdict.CONTRADICTED
    assert len(verdict.contradictions) == 1
    assert "100,000" in verdict.contradictions[0]


@pytest.mark.asyncio
async def test_contradicted_claim_negation() -> None:
    response_json = """{
      "verdict": "contradicted",
      "reason_code": "CONTRADICTION_FOUND",
      "support_spans": [],
      "contradictions": ["merger was terminated on August 1"]
    }"""
    provider = FakeLLMProvider([response_json])
    verifier = GemmaSemanticVerifier(provider)

    evidence = [_make_evidence("ch-1", "The merger was terminated on August 1.")]
    verdict = await verifier.verify_claim("The merger was successfully completed.", evidence)

    assert verdict.verdict == Verdict.CONTRADICTED
    assert len(verdict.contradictions) == 1


# ---------------------------------------------------------------------------
# Bounded repair: recheck loop for partially supported
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partially_supported_recheck_moves_to_supported() -> None:
    resp1 = """{
      "verdict": "partially_supported",
      "reason_code": "PARTIAL_SUPPORT",
      "support_spans": [],
      "contradictions": []
    }"""
    resp2 = """{
      "verdict": "supported",
      "reason_code": "DIRECT_SUPPORT",
      "support_spans": [{"evidence_id": "ch-1", "start": 0, "end": 15}],
      "contradictions": []
    }"""
    provider = FakeLLMProvider([resp1, resp2])
    verifier = GemmaSemanticVerifier(provider)

    claim = Claim(text="Partial claim", chunk_ids=["ch-1"])
    citation = Citation(chunk_id="ch-1", content="Evidence text confirming the claim fully.")
    integrity = _make_integrity_report([citation])

    report = await verify_semantic_claims([claim], integrity, verifier)

    assert report.all_supported
    assert not report.abstain_recommended
    assert report.claim_verdicts[0].verdict == Verdict.SUPPORTED
    assert len(provider.calls) == 2  # Original call + 1 recheck


@pytest.mark.asyncio
async def test_partially_supported_recheck_bounded_at_one() -> None:
    resp1 = """{"verdict": "partially_supported", "reason_code": "PARTIAL_SUPPORT"}"""
    resp2 = """{"verdict": "partially_supported", "reason_code": "STILL_PARTIAL"}"""
    provider = FakeLLMProvider([resp1, resp2])
    verifier = GemmaSemanticVerifier(provider)

    claim = Claim(text="Ambiguous claim", chunk_ids=["ch-1"])
    citation = Citation(chunk_id="ch-1", content="Some ambiguous evidence.")
    integrity = _make_integrity_report([citation])

    report = await verify_semantic_claims([claim], integrity, verifier)

    assert not report.all_supported
    assert report.claim_verdicts[0].verdict == Verdict.PARTIALLY_SUPPORTED
    assert len(provider.calls) == 2  # Exactly 1 recheck, never loops


# ---------------------------------------------------------------------------
# Fail-closed error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_closed_upstream_429() -> None:
    provider = FakeLLMProvider([_429Error("HTTP 429 rate limit exceeded")])
    verifier = GemmaSemanticVerifier(provider)

    evidence = [_make_evidence("ch-1", "Evidence.")]
    verdict = await verifier.verify_claim("Claim", evidence)

    assert verdict.verdict == Verdict.UNCERTAIN
    assert verdict.reason_code == REASON_UPSTREAM_429_EXHAUSTED


@pytest.mark.asyncio
async def test_fail_closed_timeout() -> None:
    async def _slow_generate(req: GenerationRequest) -> GenerationResult:
        await asyncio.sleep(0.5)
        return GenerationResult(text="{}", model=req.model)

    class SlowProvider(LLMProvider):
        async def generate(self, req: GenerationRequest) -> GenerationResult:
            return await _slow_generate(req)

        async def stream(self, req: GenerationRequest):
            res = await self.generate(req)
            yield res.text

    verifier = GemmaSemanticVerifier(SlowProvider(), timeout_seconds=0.05)
    evidence = [_make_evidence("ch-1", "Evidence.")]
    verdict = await verifier.verify_claim("Claim", evidence)

    assert verdict.verdict == Verdict.UNCERTAIN
    assert verdict.reason_code == REASON_TIMEOUT


@pytest.mark.asyncio
async def test_fail_closed_model_refusal() -> None:
    provider = FakeLLMProvider([_ModelRefusedError("I cannot assist with this request")])
    verifier = GemmaSemanticVerifier(provider)

    evidence = [_make_evidence("ch-1", "Evidence.")]
    verdict = await verifier.verify_claim("Claim", evidence)

    assert verdict.verdict == Verdict.UNCERTAIN
    assert verdict.reason_code == REASON_MODEL_REFUSED


@pytest.mark.asyncio
async def test_fail_closed_malformed_json() -> None:
    provider = FakeLLMProvider(["Here is my answer: verdict is supported."])
    verifier = GemmaSemanticVerifier(provider)

    evidence = [_make_evidence("ch-1", "Evidence.")]
    verdict = await verifier.verify_claim("Claim", evidence)

    assert verdict.verdict == Verdict.UNCERTAIN
    assert verdict.reason_code == REASON_MALFORMED_JSON


@pytest.mark.asyncio
async def test_fail_closed_unexpected_exception() -> None:
    provider = FakeLLMProvider([RuntimeError("Transport broken connection")])
    verifier = GemmaSemanticVerifier(provider)

    evidence = [_make_evidence("ch-1", "Evidence.")]
    verdict = await verifier.verify_claim("Claim", evidence)

    assert verdict.verdict == Verdict.UNCERTAIN
    assert verdict.reason_code == REASON_VERIFIER_OUTAGE


# ---------------------------------------------------------------------------
# Local span and ID validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_span_validation_bounds() -> None:
    response_json = """{
      "verdict": "supported",
      "reason_code": "DIRECT_SUPPORT",
      "support_spans": [
        {"evidence_id": "ch-1", "start": 0, "end": 10},
        {"evidence_id": "ch-1", "start": 50, "end": 100},
        {"evidence_id": "ch-fake", "start": 0, "end": 5}
      ]
    }"""
    provider = FakeLLMProvider([response_json])
    verifier = GemmaSemanticVerifier(provider)

    # Content length is only 20 chars
    evidence = [_make_evidence("ch-1", "Short text sample.")]
    verdict = await verifier.verify_claim("Claim", evidence)

    assert verdict.verdict == Verdict.SUPPORTED
    # Out of bounds span (50-100) and nonexistent chunk (ch-fake) must be pruned
    assert len(verdict.support_spans) == 1
    assert verdict.support_spans[0].evidence_id == "ch-1"
    assert verdict.support_spans[0].start == 0
    assert verdict.support_spans[0].end == 10


# ---------------------------------------------------------------------------
# Final publication gate evaluation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publication_gate_verified_pass() -> None:
    cit = Citation(chunk_id="ch-1", content="Evidence")
    integrity = _make_integrity_report([cit])
    sem_report = SemanticVerificationReport(
        all_supported=True,
        claim_verdicts=[
            ClaimVerdict(
                claim_id="c1",
                verdict=Verdict.SUPPORTED,
                evidence_ids=["ch-1"],
                reason_code=REASON_SUPPORTED,
            )
        ],
        abstain_recommended=False,
    )
    lookup = FakeChunkLookup({"ch-1": cit})

    gate = await evaluate_publication_gate(
        [Claim(text="c1", chunk_ids=["ch-1"])],
        integrity,
        sem_report,
        chunk_lookup=lookup,
    )

    assert gate.can_publish
    assert gate.status == PublicationStatus.VERIFIED
    assert gate.stage11_valid
    assert gate.stage12_valid


@pytest.mark.asyncio
async def test_publication_gate_stage11_failure() -> None:
    bad_citation = CandidateCitation(chunk_id="ch-fake")
    rej = RejectedCitation(citation=bad_citation, reason=RejectionReason.FABRICATED_REFERENCE)
    integrity = _make_integrity_report([], rejected=[rej])
    sem_report = SemanticVerificationReport(
        all_supported=True,
        claim_verdicts=[],
        abstain_recommended=False,
    )

    gate = await evaluate_publication_gate([], integrity, sem_report)

    assert not gate.can_publish
    assert gate.status == PublicationStatus.REJECTED
    assert not gate.stage11_valid
    assert any("FABRICATED_REFERENCE" in r for r in gate.reasons)


@pytest.mark.asyncio
async def test_publication_gate_stage12_contradiction_abstains() -> None:
    cit = Citation(chunk_id="ch-1", content="Evidence")
    integrity = _make_integrity_report([cit])
    sem_report = SemanticVerificationReport(
        all_supported=False,
        claim_verdicts=[
            ClaimVerdict(
                claim_id="c1",
                verdict=Verdict.CONTRADICTED,
                evidence_ids=["ch-1"],
                contradictions=["negation detected"],
            )
        ],
        abstain_recommended=True,
        contradictions=["negation detected"],
    )

    gate = await evaluate_publication_gate([], integrity, sem_report)

    assert not gate.can_publish
    assert gate.status == PublicationStatus.ABSTAIN
    assert "negation detected" in gate.disclosures


@pytest.mark.asyncio
async def test_publication_gate_verifier_outage_needs_review() -> None:
    cit = Citation(chunk_id="ch-1", content="Evidence")
    integrity = _make_integrity_report([cit])
    sem_report = SemanticVerificationReport(
        all_supported=False,
        claim_verdicts=[
            ClaimVerdict(
                claim_id="c1",
                verdict=Verdict.UNCERTAIN,
                evidence_ids=["ch-1"],
                reason_code=REASON_VERIFIER_OUTAGE,
            )
        ],
        abstain_recommended=True,
    )

    gate = await evaluate_publication_gate([], integrity, sem_report)

    assert not gate.can_publish
    assert gate.status == PublicationStatus.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_publication_gate_lifecycle_deletion_during_verification() -> None:
    cit = Citation(chunk_id="ch-1", content="Evidence")
    integrity = _make_integrity_report([cit])
    sem_report = SemanticVerificationReport(
        all_supported=True,
        claim_verdicts=[
            ClaimVerdict(
                claim_id="c1",
                verdict=Verdict.SUPPORTED,
                evidence_ids=["ch-1"],
            )
        ],
        abstain_recommended=False,
    )
    # Store reports ch-1 is missing (deleted or superseded between retrieval and publication)
    lookup = FakeChunkLookup({})

    gate = await evaluate_publication_gate(
        [Claim(text="c1", chunk_ids=["ch-1"])],
        integrity,
        sem_report,
        chunk_lookup=lookup,
    )

    assert not gate.can_publish
    assert gate.status == PublicationStatus.REJECTED
    assert any("DOCUMENT_MUTATED_OR_DELETED_DURING_VERIFICATION" in r for r in gate.reasons)
