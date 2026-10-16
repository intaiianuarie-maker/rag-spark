"""Stage 12 — Gemma semantic citation verifier (orchestrator + adapter).

Hermetic, offline: the orchestrator (:func:`verify_semantic_claims`) is tested
with an in-process fake verifier; the Gemma adapter's response-parsing helpers
are tested directly with canned string payloads (no model server, no API keys).

Covers every typed reason code in :mod:`answer.semantic_verification` and the
adapter's fail-closed mapping, plus the bounded-repair contract.
"""

from __future__ import annotations

import ast
import inspect
import logging
from typing import Any

import pytest
from answer.citation_integrity import CitationIntegrityReport
from answer.models import Citation, Claim
from answer.semantic_verification import (
    REASON_CONTRADICTION_FOUND,
    REASON_MALFORMED_JSON,
    REASON_MODEL_REFUSED,
    REASON_NO_EVIDENCE,
    REASON_TIMEOUT,
    REASON_UPSTREAM_429_EXHAUSTED,
    REASON_VERIFIER_OUTAGE,
    CanonicalEvidence,
    ClaimVerdict,
    SemanticClaimVerifier,
    Verdict,
    verify_semantic_claims,
)
from models.interfaces import GenerationRequest, GenerationResult, LLMProvider

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

CONTENT = "The launch budget is 20 dollars."


def _citation(
    chunk_id: str = "ch-1",
    *,
    content: str = CONTENT,
    **kw: Any,
) -> Citation:
    return Citation(chunk_id=chunk_id, content=content, **kw)


def _evidence(
    chunk_id: str = "ch-1",
    *,
    content: str = CONTENT,
) -> CanonicalEvidence:
    return CanonicalEvidence(evidence_id=chunk_id, content=content)


class FakeVerifier:
    """In-process stand-in for :class:`SemanticClaimVerifier`.

    Records every call and returns queued verdicts (or raises) in order.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[CanonicalEvidence], dict]] = []
        self._queue: list = []  # verdicts or Exception instances

    def queue(self, item: object) -> None:
        self._queue.append(item)

    async def verify_claim(
        self,
        claim_text: str,
        evidence: list[CanonicalEvidence],
        *,
        recheck: bool = False,
    ) -> ClaimVerdict:
        self.calls.append((claim_text, evidence, {"recheck": recheck}))
        if not self._queue:
            raise AssertionError(f"unexpected call for claim: {claim_text!r}")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, ClaimVerdict)
        return item


def _make_report(accepted_citations: list[Citation]) -> CitationIntegrityReport:
    return CitationIntegrityReport(
        valid=True, accepted_citations=accepted_citations
    )


@pytest.fixture
def fake_verifier() -> FakeVerifier:
    return FakeVerifier()


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def test_protocol_is_runtime_checkable() -> None:
    """SemanticClaimVerifier is a structural Protocol usable with isinstance."""
    assert isinstance(FakeVerifier(), SemanticClaimVerifier)


# ---------------------------------------------------------------------------
# verify_semantic_claims — happy path
# ---------------------------------------------------------------------------


async def test_all_supported_yields_clean_report(fake_verifier: FakeVerifier) -> None:
    """Every claim supported → all_supported True, abstain recommended False."""
    cit = _citation()
    report = _make_report([cit])

    fake_verifier.queue(
        ClaimVerdict(
            claim_id="budget",
            verdict=Verdict.SUPPORTED,
            evidence_ids=["ch-1"],
        )
    )

    result = await verify_semantic_claims(
        claims=[Claim(text="budget", chunk_ids=["ch-1"])],
        integrity_report=report,
        verifier=fake_verifier,
    )

    assert result.all_supported is True
    assert result.abstain_recommended is False
    assert result.contradictions == []
    assert len(result.claim_verdicts) == 1
    assert result.claim_verdicts[0].verdict == Verdict.SUPPORTED


# ---------------------------------------------------------------------------
# verify_semantic_claims — no surviving evidence
# ---------------------------------------------------------------------------


async def test_claim_without_surviving_evidence_is_unsupported(
    fake_verifier: FakeVerifier,
) -> None:
    """A claim whose chunk_ids are not in accepted_citations → UNSUPPORTED,
    no verifier call."""
    report = _make_report([_citation("ch-1")])

    result = await verify_semantic_claims(
        claims=[Claim(text="budget", chunk_ids=["missing-id"])],
        integrity_report=report,
        verifier=fake_verifier,
    )

    assert result.all_supported is False
    assert result.abstain_recommended is True
    ver = result.claim_verdicts[0]
    assert ver.verdict == Verdict.UNSUPPORTED
    assert ver.reason_code == REASON_NO_EVIDENCE
    assert ver.evidence_ids == ["missing-id"]
    assert fake_verifier.calls == []


# ---------------------------------------------------------------------------
# verify_semantic_claims — fail-closed (verifier raises)
# ---------------------------------------------------------------------------


async def test_verifier_exception_maps_to_uncertain(
    fake_verifier: FakeVerifier, caplog: pytest.LogCaptureFixture,
) -> None:
    """Any verifier exception → UNCERTAIN, abstain_recommended True, logged."""
    cit = _citation()
    report = _make_report([cit])

    fake_verifier.queue(RuntimeError("connection refused"))

    with caplog.at_level(logging.ERROR):
        result = await verify_semantic_claims(
            claims=[Claim(text="budget", chunk_ids=["ch-1"])],
            integrity_report=report,
            verifier=fake_verifier,
        )

    assert result.abstain_recommended is True
    assert result.all_supported is False
    ver = result.claim_verdicts[0]
    assert ver.verdict == Verdict.UNCERTAIN
    assert ver.reason_code == REASON_VERIFIER_OUTAGE


async def test_timeout_maps_to_uncertain() -> None:
    """TimeoutError → UNCERTAIN + REASON_TIMEOUT."""
    report = _make_report([_citation()])
    verifier = FakeVerifier()
    verifier.queue(TimeoutError("slow"))

    result = await verify_semantic_claims(
        claims=[Claim(text="c", chunk_ids=["ch-1"])],
        integrity_report=report,
        verifier=verifier,
    )

    ver = result.claim_verdicts[0]
    assert ver.verdict == Verdict.UNCERTAIN
    assert ver.reason_code == REASON_TIMEOUT


# ---------------------------------------------------------------------------
# verify_semantic_claims — contradictions
# ---------------------------------------------------------------------------


async def test_contradicted_claim_propagates_contradictions(
    fake_verifier: FakeVerifier,
) -> None:
    """Contradictions from a CONTRADICTED verdict are aggregated in the report."""
    cit = _citation()
    report = _make_report([cit])

    fake_verifier.queue(
        ClaimVerdict(
            claim_id="budget",
            verdict=Verdict.CONTRADICTED,
            evidence_ids=["ch-1"],
            contradictions=["the opposite of 20 dollars"],
            reason_code=REASON_CONTRADICTION_FOUND,
        )
    )

    result = await verify_semantic_claims(
        claims=[Claim(text="budget", chunk_ids=["ch-1"])],
        integrity_report=report,
        verifier=fake_verifier,
    )

    assert result.abstain_recommended is True
    assert "the opposite of 20 dollars" in result.contradictions



# ---------------------------------------------------------------------------
# verify_semantic_claims — bounded repair (partially_supported)
# ---------------------------------------------------------------------------


async def test_partial_supported_triggers_one_recheck(
    fake_verifier: FakeVerifier,
) -> None:
    """A partially_supported claim triggers exactly one recheck call."""
    cit = _citation()
    report = _make_report([cit])

    fake_verifier.queue(
        ClaimVerdict(
            claim_id="budget",
            verdict=Verdict.PARTIALLY_SUPPORTED,
            evidence_ids=["ch-1"],
        )
    )
    # recheck moves to fully supported
    fake_verifier.queue(
        ClaimVerdict(
            claim_id="budget",
            verdict=Verdict.SUPPORTED,
            evidence_ids=["ch-1"],
        )
    )

    result = await verify_semantic_claims(
        claims=[Claim(text="budget", chunk_ids=["ch-1"])],
        integrity_report=report,
        verifier=fake_verifier,
    )

    assert len(fake_verifier.calls) == 2
    assert fake_verifier.calls[0][2]["recheck"] is False
    assert fake_verifier.calls[1][2]["recheck"] is True
    assert result.all_supported is True
    assert result.abstain_recommended is False


async def test_partial_recheck_stays_partial_keeps_original(
    fake_verifier: FakeVerifier,
) -> None:
    """Recheck that stays partially_supported keeps the original verdict."""
    cit = _citation()
    report = _make_report([cit])

    original = ClaimVerdict(
        claim_id="budget",
        verdict=Verdict.PARTIALLY_SUPPORTED,
        evidence_ids=["ch-1"],
    )
    recheck = ClaimVerdict(
        claim_id="budget",
        verdict=Verdict.PARTIALLY_SUPPORTED,
        evidence_ids=["ch-1"],
    )
    fake_verifier.queue(original)
    fake_verifier.queue(recheck)

    result = await verify_semantic_claims(
        claims=[Claim(text="budget", chunk_ids=["ch-1"])],
        integrity_report=report,
        verifier=fake_verifier,
    )

    assert len(fake_verifier.calls) == 2
    assert result.claim_verdicts[0].verdict == Verdict.PARTIALLY_SUPPORTED
    assert result.abstain_recommended is True


async def test_partial_recheck_raises_keeps_original(
    fake_verifier: FakeVerifier,
) -> None:
    """If recheck itself raises, the partial verdict is preserved."""
    cit = _citation()
    report = _make_report([cit])

    fake_verifier.queue(
        ClaimVerdict(
            claim_id="budget",
            verdict=Verdict.PARTIALLY_SUPPORTED,
            evidence_ids=["ch-1"],
        )
    )
    fake_verifier.queue(RuntimeError("recheck failed"))

    result = await verify_semantic_claims(
        claims=[Claim(text="budget", chunk_ids=["ch-1"])],
        integrity_report=report,
        verifier=fake_verifier,
    )

    assert len(fake_verifier.calls) == 2
    assert result.claim_verdicts[0].verdict == Verdict.PARTIALLY_SUPPORTED




# ---------------------------------------------------------------------------
# verify_semantic_claims — no claims / empty evidence
# ---------------------------------------------------------------------------


async def test_no_claims_yields_trivial_report(
    fake_verifier: FakeVerifier,
) -> None:
    """Zero claims → trivial report with no verifier calls."""
    result = await verify_semantic_claims(
        claims=[],
        integrity_report=_make_report([]),
        verifier=fake_verifier,
    )
    assert result.all_supported is True
    assert result.abstain_recommended is False
    assert result.claim_verdicts == []
    assert fake_verifier.calls == []


# ---------------------------------------------------------------------------
# Response parsing helpers (adapter)
# ---------------------------------------------------------------------------


def test_parse_response_valid_json() -> None:
    from answer.adapters.gemma_semantic_verifier import _parse_response

    raw = '{"verdict": "supported", "reason": "SUPPORTED", "contradictions": []}'
    parsed = _parse_response(raw, [_evidence()])
    assert parsed["verdict"] == Verdict.SUPPORTED
    assert parsed["reason_code"] == "SUPPORTED"
    assert parsed["contradictions"] == []


def test_parse_response_contradicted_with_spans() -> None:
    from answer.adapters.gemma_semantic_verifier import _parse_response

    raw = (
        '{"verdict": "contradicted", "reason": "CONTRADICTION_FOUND", '
        '"contradictions": ["evidence says 30 dollars", "not 20"]}'
    )
    parsed = _parse_response(raw, [_evidence()])
    assert parsed["verdict"] == Verdict.CONTRADICTED
    assert parsed["contradictions"] == ["evidence says 30 dollars", "not 20"]


def test_parse_response_strips_surrounding_prose() -> None:
    from answer.adapters.gemma_semantic_verifier import _parse_response

    raw = (
        'Some preamble\n'
        '{"verdict": "unsupported", "reason": "NO_EVIDENCE"}\n'
        'Trailing text.'
    )
    parsed = _parse_response(raw, [_evidence()])
    assert parsed["verdict"] == Verdict.UNSUPPORTED


def test_parse_response_no_json_raises_malformed() -> None:
    from answer.adapters.gemma_semantic_verifier import (
        _MalformedJSONError,
        _parse_response,
    )

    with pytest.raises(_MalformedJSONError):
        _parse_response("no json here at all", [])


def test_parse_response_invalid_json_raises_malformed() -> None:
    from answer.adapters.gemma_semantic_verifier import (
        _MalformedJSONError,
        _parse_response,
    )

    with pytest.raises(_MalformedJSONError):
        _parse_response('{"verdict": "supported", "reason":', [])


def test_parse_response_unknown_verdict_raises() -> None:
    from answer.adapters.gemma_semantic_verifier import (
        _parse_response,
        _UnknownVerdictError,
    )

    with pytest.raises(_UnknownVerdictError):
        _parse_response('{"verdict": "maybe", "reason": "UNKNOWN"}', [])


def test_parse_response_missing_reason_uses_default() -> None:
    from answer.adapters.gemma_semantic_verifier import _parse_response

    raw = '{"verdict": "supported", "contradictions": []}'
    parsed = _parse_response(raw, [_evidence()])
    assert parsed["reason_code"] == "SUPPORTED"


def test_parse_response_normalises_contradictions() -> None:
    from answer.adapters.gemma_semantic_verifier import _parse_response

    raw = '{"verdict": "contradicted", "contradictions": "single string"}'
    parsed = _parse_response(raw, [_evidence()])
    assert parsed["contradictions"] == []


def test_fail_closed_builds_uncertain_verdict() -> None:
    from answer.adapters.gemma_semantic_verifier import _fail_closed

    verdict = _fail_closed("claim", [_evidence()], REASON_VERIFIER_OUTAGE)
    assert verdict.verdict == Verdict.UNCERTAIN
    assert verdict.reason_code == REASON_VERIFIER_OUTAGE
    assert verdict.evidence_ids == ["ch-1"]



# ---------------------------------------------------------------------------
# GemmaSemanticVerifier — fail-closed mapping
# ---------------------------------------------------------------------------


class _FakeProvider(LLMProvider):
    """Minimal LLMProvider stub."""

    def __init__(
        self, return_text: str = "", *, exc: Exception | None = None
    ) -> None:
        self.return_text = return_text
        self.exc = exc
        self.calls: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls.append(request)
        if self.exc is not None:
            raise self.exc
        return GenerationResult(text=self.return_text, model="gemma-test")

    async def stream(self, request: GenerationRequest):
        res = await self.generate(request)
        yield res.text


async def test_gemma_verifier_supported() -> None:
    from answer.adapters.gemma_semantic_verifier import GemmaSemanticVerifier

    provider = _FakeProvider(
        return_text='{"verdict": "supported", "reason": "SUPPORTED"}'
    )
    verifier = GemmaSemanticVerifier(provider)

    result = await verifier.verify_claim("budget", [_evidence()])

    assert result.verdict == Verdict.SUPPORTED
    assert result.reason_code == "SUPPORTED"


async def test_gemma_verifier_malformed_json_fail_closed() -> None:
    from answer.adapters.gemma_semantic_verifier import GemmaSemanticVerifier

    provider = _FakeProvider(return_text="I refuse to answer.")
    verifier = GemmaSemanticVerifier(provider)

    result = await verifier.verify_claim("budget", [_evidence()])

    assert result.verdict == Verdict.UNCERTAIN
    assert result.reason_code == REASON_MALFORMED_JSON


async def test_gemma_verifier_timeout_fail_closed() -> None:
    from answer.adapters.gemma_semantic_verifier import GemmaSemanticVerifier

    provider = _FakeProvider(exc=TimeoutError("slow"))
    verifier = GemmaSemanticVerifier(provider)

    result = await verifier.verify_claim("budget", [_evidence()])

    assert result.verdict == Verdict.UNCERTAIN
    assert result.reason_code == REASON_TIMEOUT


async def test_gemma_verifier_429_fail_closed() -> None:
    from answer.adapters.gemma_semantic_verifier import (
        GemmaSemanticVerifier,
        _429Error,
    )

    provider = _FakeProvider(exc=_429Error("rate limited"))
    verifier = GemmaSemanticVerifier(provider)

    result = await verifier.verify_claim("budget", [_evidence()])

    assert result.verdict == Verdict.UNCERTAIN
    assert result.reason_code == REASON_UPSTREAM_429_EXHAUSTED


async def test_gemma_verifier_refusal_fail_closed() -> None:
    from answer.adapters.gemma_semantic_verifier import (
        GemmaSemanticVerifier,
        _ModelRefusedError,
    )

    provider = _FakeProvider(exc=_ModelRefusedError("blocked"))
    verifier = GemmaSemanticVerifier(provider)

    result = await verifier.verify_claim("budget", [_evidence()])

    assert result.verdict == Verdict.UNCERTAIN
    assert result.reason_code == REASON_MODEL_REFUSED


async def test_gemma_verifier_unknown_error_fail_closed() -> None:
    from answer.adapters.gemma_semantic_verifier import GemmaSemanticVerifier

    provider = _FakeProvider(exc=ValueError("unexpected"))
    verifier = GemmaSemanticVerifier(provider)

    result = await verifier.verify_claim("budget", [_evidence()])

    assert result.verdict == Verdict.UNCERTAIN
    assert result.reason_code == REASON_VERIFIER_OUTAGE


async def test_gemma_verifier_recheck_uses_narrow_prompt() -> None:
    from answer.adapters.gemma_semantic_verifier import GemmaSemanticVerifier

    captured: list[str] = []

    class _SpyProvider(LLMProvider):
        async def generate(self, request: GenerationRequest) -> GenerationResult:
            msgs = request.messages
            captured.append(msgs[0].content)
            return GenerationResult(
                text='{"verdict": "supported"}', model="g"
            )

        async def stream(self, request: GenerationRequest):
            res = await self.generate(request)
            yield res.text

    verifier = GemmaSemanticVerifier(_SpyProvider())
    await verifier.verify_claim("budget", [_evidence()], recheck=True)

    assert "re-verifying" in captured[0].lower()


# ---------------------------------------------------------------------------
# Hermeticity guard
# ---------------------------------------------------------------------------

_FORBIDDEN_MODULE_PREFIXES = {
    "httpx",
    "requests",
    "aiohttp",
    "urllib3",
    "openai",
    "anthropic",
    "litellm",
    "vertexai",
    "boto3",
    "redis",
    "psycopg",
}



def test_semantic_verification_module_is_hermetic() -> None:
    """semantic_verification must not import any LLM / network / DB client.

    Stage 12's *orchestrator* is pure logic — the model is behind the
    SemanticClaimVerifier protocol seam, so the module's import graph must stay
    service-free (matching Stage 11's hermeticity contract).
    """
    import answer.semantic_verification as mod

    tree = ast.parse(inspect.getsource(mod))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
    violations = sorted(n for n in imported if n in _FORBIDDEN_MODULE_PREFIXES)
    assert not violations, (
        f"semantic_verification imports a forbidden dependency: {violations}"
    )
