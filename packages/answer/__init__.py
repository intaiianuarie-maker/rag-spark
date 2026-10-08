"""Answer engine package (rag-fork-solution TASK-M4-002).

Product glue that composes reused components behind stable interfaces:

    orchestrator.retrieve()  ->  grounded prompt  ->  LLMProvider.generate()
                             ->  parse {answer, claims[{text, chunk_ids}]}
                             ->  verify each chunk_id (exists AND in-ACL)
                             ->  Answer(answer, claims, citations)

Nothing here reimplements retrieval, fusion, embeddings or generation — those
are reused behind `retrieval.interfaces` / `models.interfaces`. This package
owns the prompt, the structured-output parsing and citation verification, which
are product-specific (provenance, ACL, citation UX).
"""

from __future__ import annotations

from .engine import AnswerEngine, AnswerEngineError
from .interfaces import ChunkLookup
from .models import Answer, Citation, Claim

__all__ = [
    "Answer",
    "AnswerEngine",
    "AnswerEngineError",
    "ChunkLookup",
    "Citation",
    "Claim",
]
