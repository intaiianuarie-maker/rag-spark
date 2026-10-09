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
  :mod:`answer.verification`).
- **Typed failures**: an LLM provider error propagates as the provider's typed
  error so the route can map it (503); an unparseable model response falls back
  to the raw text with no claims rather than inventing structure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.auth import ACLScope
from core.logging import request_id_ctx
from models.interfaces import (
    ChatMessage,
    GenerationRequest,
    LLMProvider,
    MessageRole,
)
from retrieval.interfaces import (
    DEFAULT_CONTEXT_TOP_K,
    RetrievalOrchestrator,
)

from .interfaces import ChunkLookup
from .models import Answer, AnswerStatus, Claim, Confidence, ConfidenceLevel
from .prompts import SYSTEM_PROMPT, build_prompt, parse_model_output
from .verification import verify_claims

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

    async def answer(self, *, query: str, acl_scope: ACLScope) -> Answer:
        """Retrieve, generate, parse and verify a grounded answer."""
        items = await self.orchestrator.retrieve(
            query=query,
            acl_scope=acl_scope,
            context_top_k=self.max_context_items,
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
        result = await self.llm.generate(request)

        try:
            parsed = parse_model_output(result.text)
        except ValueError as exc:
            # Never fabricate structure: return the raw text, no claims.
            logger.warning(
                "answer_parse_fallback",
                extra={"event": "answer_parse_fallback", "error": str(exc)},
            )
            return Answer(
                answer=result.text.strip(),
                status=AnswerStatus.PARTIAL,
                reason_code="PARSE_FALLBACK",
                reason=(
                    "Model response could not be parsed into structured claims; "
                    "raw response preserved."
                ),
                confidence=Confidence(
                    level=ConfidenceLevel.LOW,
                    basis="Unstructured model generation",
                ),
                claims=[],
                citations=[],
            )

        claims = [Claim(text=c["text"], chunk_ids=c["chunk_ids"]) for c in parsed["claims"]]
        allowed: set[str] | None = set(acl_scope.workspace_ids)
        if acl_scope.include_public:
            allowed = None  # public scope: existence is the only check
        verified, citations = await verify_claims(
            claims, items, chunk_lookup=self.chunk_lookup, allowed_workspace_ids=allowed
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

        trace_id = request_id_ctx.get()

        if not items or is_refusal_phrase:
            return Answer(
                answer=answer_text,
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
            return Answer(
                answer=answer_text,
                status=AnswerStatus.PARTIAL,
                reason_code="PARTIAL_EVIDENCE",
                reason=f"Only {len(verified)} of {len(claims)} claim(s) could be verified with valid citations.",
                confidence=Confidence(
                    level=ConfidenceLevel.MEDIUM,
                    basis=f"Partial verification: {len(verified)}/{len(claims)} claim(s) supported",
                ),
                trace_id=trace_id,
                claims=verified,
                citations=citations,
            )

        has_high_conf = bool(citations) and (len(verified) == len(claims) if claims else False)
        confidence_level = ConfidenceLevel.HIGH if has_high_conf else ConfidenceLevel.MEDIUM
        return Answer(
            answer=answer_text,
            status=AnswerStatus.ANSWERED,
            reason_code=None,
            reason=None,
            confidence=Confidence(
                level=confidence_level,
                basis=f"Answer grounded in {len(citations)} verified citation(s)",
            ),
            trace_id=trace_id,
            claims=verified,
            citations=citations,
        )
