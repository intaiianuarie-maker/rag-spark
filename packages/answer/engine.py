"""Answer engine (rag-fork-solution TASK-M4-002) — the custom product glue.

Pipeline (all heavy lifting reused behind interfaces):

    orchestrator.retrieve()  ->  grounded prompt  ->  llm.generate()
                             ->  parse {answer, claims[{text, chunk_ids}]}
                             ->  verify each chunk_id (exists AND in-ACL)
                             ->  Answer(answer, claims, citations)

Guarantees:

- **Grounded**: the prompt contains only retrieved, in-scope context and
  instructs the model to copy ``chunk_id``s verbatim.
- **No fabrication**: every claim ``chunk_id`` is verified; invalid ids are
  stripped and claims left with no valid id are dropped (see
  :mod:`answer.verification`). When no claims survive verification — or the
  model produced no claims at all — the engine abstains with an
  ``INSUFFICIENT_EVIDENCE`` answer rather than echoing unverified prose.
- **Grounded rendering**: for partially-verified answers, only the text of
  verified claims is returned; unsupported sentences from the model are
  discarded.
- **Conservative extractive grounding** (v1 interim): a claim is cited only
  when its text appears **verbatim** in the cited chunk
  (:class:`~answer.verification.ExtractiveGrounder`, default). This rejects
  negation-stripping, entity/role swap and value/date changes that a
  token-overlap scorer cannot see, and never grants ``HIGH`` on paraphrase it
  cannot verify. Confidence is honest: ``HIGH`` is granted only when every
  surviving claim is grounded. **Limitation**: cross-language translation is
  *not* supported by this interim mode (a translation's text is not a substring
  of a source in another language); that is the separate multilingual release
  gate.
- **Typed failures**: an LLM provider error propagates as the provider's typed
  error so the route can map it (503); unparseable or claimless model output
  abstains with ``INSUFFICIENT_EVIDENCE`` rather than returning raw prose.
- **Honest leg statuses (T-V1-026)**: retrieval outcomes are read from the
  orchestrator's context-local report, so a **required** retrieval leg failing
  returns ``UNAVAILABLE`` with the registered safe reason and the tracking ID
  — the model is never called on an unsearched corpus, and a down service is
  never reported as "insufficient evidence". An **optional** leg (reranker)
  downgrades a standing answer to ``DEGRADED`` with the degradation disclosed.
  An **empty corpus is unchanged**: real abstention with ``EVIDENCE_MISSING``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from core.auth import ACLScope
from core.errors import ReasonCode
from core.logging import request_id_ctx
from models.interfaces import (
    ChatMessage,
    GenerationRequest,
    LLMProvider,
    MessageRole,
)
from retrieval.interfaces import (
    DEFAULT_CONTEXT_TOP_K,
    RetrievalMode,
    RetrievalOrchestrator,
    RetrievedItem,
)
from retrieval.orchestrator import RetrievalResult, RetrievalStatus, pop_retrieval_outcome

from .interfaces import ChunkLookup
from .models import Answer, AnswerStatus, Claim, Confidence, ConfidenceLevel
from .prompts import SYSTEM_PROMPT, build_prompt, parse_model_output
from .verification import ClaimGrounder, ExtractiveGrounder, verify_claims

logger = logging.getLogger("knowledge_os.answer")


class AnswerEngineError(RuntimeError):
    """Raised when the answer engine cannot produce an answer."""


@dataclass
class AnswerEngine:
    """Compose retrieval + generation into a verified, citation-ready answer."""

    orchestrator: RetrievalOrchestrator
    llm: LLMProvider
    model: str
    chunk_lookup: ChunkLookup | None = None
    max_context_items: int = DEFAULT_CONTEXT_TOP_K
    temperature: float | None = None
    max_tokens: int | None = None
    extra: dict = field(default_factory=dict)
    #: Optional claim-substantiation gate wired in by default
    #: (:class:`~answer.verification.ExtractiveGrounder`, conservative v1
    #: verbatim-substring check) so a citation is real *and* supports the
    #: claim. ``HIGH`` confidence is granted only when every surviving claim is
    #: grounded. Pass ``claim_grounder=None`` to restore the legacy
    #: existence+scope-only contract.
    claim_grounder: ClaimGrounder | None = field(default_factory=ExtractiveGrounder)

    async def answer(
        self, *, query: str, acl_scope: ACLScope, mode: RetrievalMode = RetrievalMode.HYBRID
    ) -> Answer:
        """Retrieve, generate, parse and verify a grounded answer.

        ``mode`` selects the retrieval leg (default ``HYBRID`` = v1 behavior);
        it is passed straight through to ``orchestrator.retrieve`` so callers
        can opt into ``GRAPH``/``HYBRID_GRAPH`` once ``GRAPH_MODE`` enables the
        graph leg (G4). ACL-before-ranking and citation verification apply to
        every mode.
        """
        items = await self.orchestrator.retrieve(
            query=query,
            acl_scope=acl_scope,
            mode=mode,
            context_top_k=self.max_context_items,
        )
        # T-V1-026: the orchestrator published the honest outcome of that call
        # on a context-local slot (works through the hydration decorator and
        # through test doubles, which publish nothing and read as healthy).
        outcome: RetrievalResult | None = pop_retrieval_outcome()
        trace_id = request_id_ctx.get() or (outcome.trace_id if outcome else None)

        if outcome is not None and outcome.status is RetrievalStatus.UNAVAILABLE:
            # A REQUIRED leg failed: the corpus was never searched, so
            # "insufficient evidence" would be a lie. Fail closed with the
            # registered safe reason + tracking ID and never call the model on
            # an empty context.
            reason_code = (
                outcome.reason_code.value if outcome.reason_code is not None else None
            )
            logger.warning(
                "retrieval_unavailable",
                extra={
                    "event": "retrieval_unavailable",
                    "reason_code": reason_code,
                    "trace_id": trace_id,
                    "legs_failed": [failure.leg for failure in outcome.legs_failed],
                },
            )
            return Answer(
                answer="I cannot answer this question right now.",
                status=AnswerStatus.UNAVAILABLE,
                reason_code=reason_code,
                reason=outcome.reason,
                confidence=Confidence(
                    level=ConfidenceLevel.LOW,
                    basis="A required retrieval leg was unavailable; nothing was searched",
                ),
                trace_id=trace_id,
                claims=[],
                citations=[],
            )

        prompt = build_prompt(query, items)
        request = GenerationRequest(
            model=self.model,
            messages=[
                ChatMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
                ChatMessage(role=MessageRole.USER, content=prompt),
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        try:
            result = await self.llm.generate(request)
        except Exception as exc:
            # Generation leg: never swallow into an abstention. The typed
            # provider error propagates so the route maps it to 503 with the
            # same tracking ID this log line carries.
            logger.warning(
                "generation_leg_failed",
                extra={
                    "event": "generation_leg_failed",
                    "error_type": type(exc).__name__,
                    "trace_id": trace_id,
                },
            )
            raise

        try:
            parsed = parse_model_output(result.text)
        except ValueError as exc:
            # Never fabricate structure or return raw prose as a factual answer.
            logger.warning(
                "answer_parse_fallback",
                extra={"event": "answer_parse_fallback", "error": str(exc), "trace_id": trace_id},
            )
            return Answer(
                answer="I cannot answer this question based on the provided documents.",
                status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                reason_code="PARSE_FALLBACK",
                reason="Model output was not valid JSON; no claims could be verified.",
                confidence=Confidence(
                    level=ConfidenceLevel.LOW,
                    basis="Model output unparseable — no structure to verify",
                ),
                trace_id=trace_id,
                claims=[],
                citations=[],
            )

        claims = [Claim(text=c["text"], chunk_ids=c["chunk_ids"]) for c in parsed["claims"]]
        allowed: set[str] | None = set(acl_scope.workspace_ids)
        if acl_scope.include_public:
            allowed = None  # public scope: existence is the only check
        verified, citations = await verify_claims(
            claims,
            items,
            chunk_lookup=self.chunk_lookup,
            allowed_workspace_ids=allowed,
            claim_grounder=self.claim_grounder,
        )

        answer_text = parsed["answer"].strip()
        lower_ans = answer_text.lower()
        is_refusal_phrase = any(
            phrase in lower_ans
            for phrase in (
                "does not contain",
                "cannot answer",
                "nu conține",
                "não contém",
                "insufficient information",
                "not enough information",
            )
        )

        if not items or is_refusal_phrase:
            return Answer(
                answer="I cannot answer this question based on the provided documents.",
                status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                reason_code="EVIDENCE_MISSING",
                reason="Accessible sources do not contain sufficient evidence to answer.",
                confidence=Confidence(
                    level=ConfidenceLevel.LOW,
                    basis="No relevant documents retrieved or model declared missing evidence",
                ),
                trace_id=trace_id,
                claims=verified,
                citations=citations,
            )

        if not claims:
            # No structured claims produced by model — cannot verify anything.
            return Answer(
                answer="I cannot answer this question based on the provided documents.",
                status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                reason_code="NO_CLAIMS",
                reason="Model response did not include verifiable claims grounded in citations.",
                confidence=Confidence(
                    level=ConfidenceLevel.LOW,
                    basis="No claims produced",
                ),
                trace_id=trace_id,
                claims=[],
                citations=[],
            )

        if claims and not verified:
            return Answer(
                answer="I cannot answer this question based on the provided documents.",
                status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                reason_code="CITATION_INVALID",
                reason="Generated claims lacked valid citations in accessible documents.",
                confidence=Confidence(
                    level=ConfidenceLevel.LOW,
                    basis="Zero verified claims survived citation integrity check",
                ),
                trace_id=trace_id,
                claims=[],
                citations=[],
            )

        if claims and len(verified) < len(claims):
            # Partial verification: render only from the verified claims.
            rendered_answer = " ".join(claim.text for claim in verified)
            return self._disclose_source(
                self._disclose(
                Answer(
                    answer=rendered_answer,
                    status=AnswerStatus.PARTIAL,
                    reason_code="PARTIAL_EVIDENCE",
                    reason=(
                        f"Only {len(verified)} of {len(claims)} "
                        "claim(s) could be verified with valid citations."
                    ),
                    confidence=Confidence(
                        level=ConfidenceLevel.MEDIUM,
                        basis=(
                            f"Partial verification: "
                            f"{len(verified)}/{len(claims)} claim(s) supported"
                        ),
                    ),
                    trace_id=trace_id,
                    claims=verified,
                    citations=citations,
                ),
                outcome,
                ),
                items,
            )

        has_high_conf = bool(citations) and (len(verified) == len(claims) if claims else False)
        confidence_level = ConfidenceLevel.HIGH if has_high_conf else ConfidenceLevel.MEDIUM
        rendered_answer = " ".join(claim.text for claim in verified)
        return self._disclose_source(
            self._disclose(
            Answer(
                answer=rendered_answer,
                status=AnswerStatus.ANSWERED,
                reason_code=None,
                reason=None,
                confidence=Confidence(
                    level=confidence_level,
                    basis=(
                        f"Answer grounded in {len(citations)} "
                        "verified, substantiated citation(s)"
                    ),
                ),
                trace_id=trace_id,
                claims=verified,
                citations=citations,
            ),
            outcome,
            ),
            items,
        )

    @staticmethod
    def _collect_lost_regions(items: Sequence[RetrievedItem]) -> list[dict]:
        """Lost regions carried by the retrieved chunks (T-V1-031).

        The ingestion pipeline writes them onto every chunk of a partially-read
        source (``ingest_pipeline.parse_disclosure``); retrieval hands them
        back on the items' metadata unchanged.
        """
        lost: list[dict] = []
        for item in items:
            for region in item.metadata.get("parse_lost_regions") or ():
                if isinstance(region, dict) and region not in lost:
                    lost.append(region)
        return lost

    @staticmethod
    def _limitation_text(lost: Sequence[dict]) -> str:
        page_summary = "; ".join(
            f"pages {region.get('lost_pages')} of {region.get('page_count')}"
            for region in lost
        )
        return (
            f"Only part of the source was read: {page_summary} carried an image "
            "and no text layer, so that content was NOT available to this answer."
        )

    @classmethod
    def _disclose_source(cls, answer: Answer, items: Sequence[RetrievedItem]) -> Answer:
        """Disclose partially-parsed sources; never let one read as confident (T-V1-031).

        The pipeline parks a job ``needs_review`` when part of a source could not
        be read (e.g. image-only PDF pages) and writes the lost regions onto
        every chunk it published. Retrieval hands that metadata straight back on
        the retrieved items, so the answer layer says the same thing the job
        said — instead of answering over an omitted region as if the document
        had been read in full.

        Two rules, both hard:

        * a **standing** answer is labelled ``needs_review`` and lists the
          regions that were never read (an abstention keeps its own reason —
          it is already not a claim to have read anything);
        * confidence is capped at ``MEDIUM``. ``HIGH`` claims completeness, and
          completeness is exactly what a partially-read source does not have.
        """
        lost = cls._collect_lost_regions(items)
        if not lost:
            return answer

        limitation = cls._limitation_text(lost)
        limitations = [*answer.limitations]
        if limitation not in limitations:
            limitations.append(limitation)

        level = (
            ConfidenceLevel.MEDIUM
            if answer.confidence.level is ConfidenceLevel.HIGH
            else answer.confidence.level
        )
        standing = answer.status in {AnswerStatus.ANSWERED, AnswerStatus.PARTIAL}
        return answer.model_copy(
            update={
                "status": AnswerStatus.NEEDS_REVIEW if standing else answer.status,
                "reason_code": (
                    ReasonCode.NEEDS_REVIEW.value if standing else answer.reason_code
                ),
                "reason": (
                    "A cited source was only partially parsed; see limitations."
                    if standing
                    else answer.reason
                ),
                "limitations": limitations,
                "confidence": Confidence(
                    level=level,
                    basis=(
                        f"{answer.confidence.basis} — capped at {level.value}: a "
                        "partially-parsed source cannot ground a complete answer"
                    ),
                ),
            }
        )

    @staticmethod
    def _disclose(answer: Answer, outcome: RetrievalResult | None) -> Answer:
        """Downgrade a standing answer to ``degraded`` when an OPTIONAL leg was down.

        T-V1-026: the answer still stands, but the user must be told the
        reranker (or another optional capability) was unavailable. The reason
        code is kept inside the set registered for ``degraded``
        (``RERANK_UNAVAILABLE`` / ``DEGRADED_RESULT``); per-leg codes such as
        ``GRAPH_UNAVAILABLE`` remain observable on the retrieval side.
        """
        if outcome is None or outcome.status is not RetrievalStatus.DEGRADED:
            return answer
        code = outcome.reason_code
        if code is None or code.value not in {"DEGRADED_RESULT", "RERANK_UNAVAILABLE"}:
            code = ReasonCode.DEGRADED_RESULT
        return answer.model_copy(
            update={
                "status": AnswerStatus.DEGRADED,
                "reason_code": code.value,
                "reason": outcome.reason,
            }
        )
