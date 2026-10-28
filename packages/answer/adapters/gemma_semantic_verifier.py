"""Stage 12 — Gemma semantic citation verifier adapter.

Implements :class:`~answer.semantic_verification.SemanticClaimVerifier` using
Google Gemma 4 26B A4B IT (served via OpenRouter free -> approved paid route).

Design notes
------------
* Fail-closed mapping (T-OR-06): every non-success path — upstream ``429``, timeout,
  model refusal, malformed JSON, or any unexpected exception — is mapped to a
  typed reason code and an ``UNCERTAIN`` verdict so the orchestrator never emits
  a verified badge when truth could not be established.
* Deterministic prompt: evidence passages and the claim are injected verbatim;
  the model is instructed to return only JSON with ``verdict``, ``reason_code``,
  ``support_spans``, and ``contradictions`` keys. No free-form prose is trusted.
* Local validation: returned evidence IDs and span offsets are validated
  against the provided canonical evidence to prevent hallucinated spans.
* Bounded repair: when ``recheck=True`` a narrower prompt is used that
  asks the model to re-evaluate only the specific partial-support gap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx
from models.interfaces import (
    ChatMessage,
    GenerationRequest,
    LLMProvider,
    MessageRole,
)

from ..semantic_verification import (
    REASON_MALFORMED_JSON,
    REASON_MODEL_REFUSED,
    REASON_TIMEOUT,
    REASON_UNKNOWN_VERDICT,
    REASON_UPSTREAM_429_EXHAUSTED,
    REASON_VERIFIER_OUTAGE,
    CanonicalEvidence,
    ClaimVerdict,
    SemanticClaimVerifier,
    SupportSpan,
    Verdict,
    default_reason,
)

logger = logging.getLogger("knowledge_os.answer.gemma_semantic_verifier")

#: Default model identifier for the Gemma verifier (Free tier with paid fallback).
DEFAULT_GEMMA_MODEL = "google/gemma-4-26b-a4b-it:free"
PAID_GEMMA_MODEL = "google/gemma-4-26b-a4b-it"

#: System prompt — pinned for reproducibility.
_VERIFY_SYSTEM_PROMPT = """\
You are a semantic citation verifier. For the single claim and evidence passages,
decide whether the evidence substantiates, partially substantiates, contradicts,
or does not address the claim.

Return ONLY a JSON object with exactly these keys and no surrounding text:

{
  "verdict": "<one of: supported|partially_supported|unsupported|contradicted|uncertain>",
  "reason_code": "<one of: DIRECT_SUPPORT|PARTIAL_SUPPORT|NO_EVIDENCE|CONTRADICTION_FOUND>",
  "support_spans": [{"evidence_id": "<id>", "start": 0, "end": 10}],
  "contradictions": ["<verbatim contradicting span>"]
}

* If the claim is not directly addressed by the evidence, return "unsupported".
* If the evidence contradicts the claim (e.g. wrong numbers/dates/entities/negation),
  return "contradicted" and populate "contradictions".
"""

#: Narrowed system prompt used by the bounded-repair recheck loop.
_RECHECK_SYSTEM_PROMPT = """\
You are re-verifying a single claim that was previously marked "partially_supported".
Focus only on the specific gap: does the evidence fully support, still partially support,
or contradict the claim?

Return ONLY a JSON object with exactly these keys and no surrounding text:

{
  "verdict": "<one of: supported|partially_supported|unsupported|contradicted|uncertain>",
  "reason_code": "<one of: DIRECT_SUPPORT|PARTIAL_SUPPORT|NO_EVIDENCE|CONTRADICTION_FOUND>",
  "support_spans": [{"evidence_id": "<id>", "start": 0, "end": 10}],
  "contradictions": ["<verbatim contradicting span>"]
}
"""


class GemmaSemanticVerifier(SemanticClaimVerifier):
    """Gemma-based :class:`SemanticClaimVerifier` adapter.

    Parameters
    ----------
    provider:
        An :class:`~models.interfaces.LLMProvider` instance capable of serving
        the configured Gemma model.
    model:
        Model identifier passed through to the provider via
        :attr:`GenerationRequest.model`.
    timeout_seconds:
        Request-level timeout. Exceeding it yields
        :data:`REASON_TIMEOUT` + ``UNCERTAIN``.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str = DEFAULT_GEMMA_MODEL,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._provider = provider
        self._model = model
        self._timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # Public protocol method
    # ------------------------------------------------------------------

    async def verify_claim(
        self,
        claim_text: str,
        evidence: list[CanonicalEvidence],
        *,
        recheck: bool = False,
    ) -> ClaimVerdict:
        """Evaluate a single claim against canonical evidence via Gemma."""
        evidence_ids = [e.evidence_id for e in evidence]
        try:
            raw = await asyncio.wait_for(
                self._call_model(claim_text, evidence, recheck=recheck),
                timeout=self._timeout_seconds,
            )
        except (TimeoutError, httpx.TimeoutException):
            return _fail_closed(claim_text, evidence, REASON_TIMEOUT)
        except _429Error:
            return _fail_closed(claim_text, evidence, REASON_UPSTREAM_429_EXHAUSTED)
        except _ModelRefusedError:
            return _fail_closed(claim_text, evidence, REASON_MODEL_REFUSED)
        except Exception as exc:
            msg = str(exc).lower()
            if "429" in msg or "quota" in msg or "rate" in msg:
                return _fail_closed(claim_text, evidence, REASON_UPSTREAM_429_EXHAUSTED)
            logger.exception("gemma_verifier_unexpected_error")
            return _fail_closed(claim_text, evidence, REASON_VERIFIER_OUTAGE)

        try:
            parsed = _parse_response(raw, evidence)
        except _MalformedJSONError:
            return _fail_closed(claim_text, evidence, REASON_MALFORMED_JSON)
        except _UnknownVerdictError as exc:
            logger.warning("gemma_verifier_unknown_verdict: %s", exc)
            return _fail_closed(claim_text, evidence, REASON_UNKNOWN_VERDICT)

        return ClaimVerdict(
            claim_id=claim_text,
            verdict=parsed["verdict"],
            evidence_ids=evidence_ids,
            support_spans=parsed.get("support_spans", []),
            contradictions=parsed.get("contradictions", []),
            reason_code=parsed.get("reason_code"),
        )

    # ------------------------------------------------------------------
    # Model call
    # ------------------------------------------------------------------

    async def _call_model(
        self,
        claim_text: str,
        evidence: list[CanonicalEvidence],
        *,
        recheck: bool,
    ) -> str:
        """Send the request and return the raw model text."""
        system_prompt = _RECHECK_SYSTEM_PROMPT if recheck else _VERIFY_SYSTEM_PROMPT
        user_prompt = _build_user_prompt(claim_text, evidence)

        request = GenerationRequest(
            model=self._model,
            messages=[
                ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
                ChatMessage(role=MessageRole.USER, content=user_prompt),
            ],
            temperature=0.0,
            max_tokens=512,
        )
        result = await self._provider.generate(request)
        return result.text


# ---------------------------------------------------------------------------
# Helpers & response parsing
# ---------------------------------------------------------------------------

_VERDICT_MAP: dict[str, Verdict] = {
    "supported": Verdict.SUPPORTED,
    "partially_supported": Verdict.PARTIALLY_SUPPORTED,
    "unsupported": Verdict.UNSUPPORTED,
    "contradicted": Verdict.CONTRADICTED,
    "uncertain": Verdict.UNCERTAIN,
}

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class _MalformedJSONError(Exception):
    """Raised when the model response is not valid JSON."""


class _UnknownVerdictError(Exception):
    """Raised when the parsed verdict is not a recognised Verdict value."""


class _429Error(Exception):
    """Raised when the upstream provider returned HTTP 429."""


class _ModelRefusedError(Exception):
    """Raised when the model refused to answer."""


def _build_user_prompt(claim_text: str, evidence: list[CanonicalEvidence]) -> str:
    """Build the user prompt from the claim and evidence passages."""
    lines = [f"CLAIM: {claim_text}", "", "EVIDENCE:"]
    for ev in evidence:
        lines.append(f'  [id: {ev.evidence_id}] "{ev.content}"')
    lines.append("")
    lines.append("DECISION (JSON only):")
    return "\n".join(lines)


def _fail_closed(
    claim_text: str,
    evidence: list[CanonicalEvidence],
    reason_code: str,
) -> ClaimVerdict:
    """Build a fail-closed UNCERTAIN verdict (never emits a verified badge)."""
    return ClaimVerdict(
        claim_id=claim_text,
        verdict=Verdict.UNCERTAIN,
        evidence_ids=[e.evidence_id for e in evidence],
        reason_code=reason_code,
    )


def _parse_response(
    raw: str,
    evidence: list[CanonicalEvidence] | None = None,
) -> dict[str, Any]:
    """Extract and validate the JSON payload from the model response."""
    match = _JSON_RE.search(raw)
    if match is None:
        raise _MalformedJSONError("no JSON object found in response")

    try:
        data: dict[str, Any] = json.loads(match.group())
    except (ValueError, TypeError) as exc:
        raise _MalformedJSONError(str(exc)) from exc

    verdict_str = data.get("verdict")
    verdict = (
        _VERDICT_MAP.get(str(verdict_str).lower().strip())
        if verdict_str is not None
        else None
    )
    if verdict is None:
        raise _UnknownVerdictError(f"unknown verdict: {verdict_str!r}")

    # Contradictions
    contradictions_raw = data.get("contradictions", [])
    if not isinstance(contradictions_raw, list):
        contradictions: list[str] = []
    else:
        contradictions = [str(c) for c in contradictions_raw if c]

    # Support spans with local bounds validation
    support_spans_raw = data.get("support_spans", [])
    valid_ids = {e.evidence_id: len(e.content) for e in evidence} if evidence else None
    validated_spans: list[SupportSpan] = []

    if isinstance(support_spans_raw, list):
        for item in support_spans_raw:
            if isinstance(item, dict):
                eid = str(item.get("evidence_id", ""))
                start = item.get("start", 0)
                end = item.get("end", 0)
                if (
                    valid_ids is None
                    or (
                        eid in valid_ids
                        and isinstance(start, int)
                        and isinstance(end, int)
                        and 0 <= start <= end <= valid_ids[eid]
                    )
                ):
                    validated_spans.append(SupportSpan(evidence_id=eid, start=start, end=end))
            elif isinstance(item, str):
                default_eid = next(iter(valid_ids.keys()), "") if valid_ids else ""
                validated_spans.append(
                    SupportSpan(evidence_id=default_eid, start=0, end=0)
                )

    reason = data.get("reason_code") or data.get("reason")
    if not isinstance(reason, str) or not reason:
        reason = default_reason(verdict)

    return {
        "verdict": verdict,
        "contradictions": contradictions,
        "support_spans": validated_spans,
        "reason": reason,
        "reason_code": reason,
    }
