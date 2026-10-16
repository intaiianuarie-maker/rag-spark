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

from .citation_integrity import (
    CitationIntegrityError,
    CitationIntegrityReport,
    RejectedCitation,
    RejectionReason,
    validate_citation_integrity,
)
from .engine import AnswerEngine, AnswerEngineError
from .interfaces import ChunkLookup
from .models import Answer, Citation, Claim
from .semantic_verification import (
    CanonicalEvidence,
    ClaimVerdict,
    PublicationGateResult,
    PublicationStatus,
    SemanticClaimVerifier,
    SemanticVerificationError,
    SemanticVerificationReport,
    SupportSpan,
    Verdict,
    evaluate_publication_gate,
    verify_semantic_claims,
)
from .verification import CrossLanguageGrounder, ExtractiveGrounder

__all__ = [
    "Answer",
    "AnswerEngine",
    "AnswerEngineError",
    "CanonicalEvidence",
    "ChunkLookup",
    "Citation",
    "CitationIntegrityError",
    "CitationIntegrityReport",
    "Claim",
    "ClaimVerdict",
    "CrossLanguageGrounder",
    "ExtractiveGrounder",
    "PublicationGateResult",
    "PublicationStatus",
    "RejectedCitation",
    "RejectionReason",
    "SemanticClaimVerifier",
    "SemanticVerificationError",
    "SemanticVerificationReport",
    "SupportSpan",
    "Verdict",
    "evaluate_publication_gate",
    "validate_citation_integrity",
    "verify_semantic_claims",
]

