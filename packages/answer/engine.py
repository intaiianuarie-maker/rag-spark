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
from .models import Answer, Claim
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
            return Answer(answer=result.text.strip(), claims=[], citations=[])

        claims = [Claim(text=c["text"], chunk_ids=c["chunk_ids"]) for c in parsed["claims"]]
        allowed: set[str] | None = set(acl_scope.workspace_ids)
        if acl_scope.include_public:
            allowed = None  # public scope: existence is the only check
        verified, citations = await verify_claims(
            claims, items, chunk_lookup=self.chunk_lookup, allowed_workspace_ids=allowed
        )
        if claims and not verified:
            return Answer(
                answer="I cannot answer this question based on the provided documents.",
                claims=[],
                citations=[],
            )
        return Answer(
            answer=parsed["answer"].strip(),
            claims=verified,
            citations=citations,
        )
