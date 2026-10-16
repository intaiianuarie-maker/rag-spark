"""Answer engine data models (TASK-M4-002).

The structured LLM output is ``{answer, claims[{text, chunk_ids}]}``; the
verified, citation-ready output is :class:`Answer`. Citations carry the
provenance the UI needs to open the exact passage (doc, filename, page,
section, span).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from core.errors import (
    AnswerStatus as AnswerStatus,  # re-export: single shared definition (T-V1-025)
)
from pydantic import BaseModel, ConfigDict, Field


class ConfidenceLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNAVAILABLE = "unavailable"


class Confidence(BaseModel):
    level: ConfidenceLevel = ConfidenceLevel.MEDIUM
    basis: str = ""
    probability: float | None = None


class Claim(BaseModel):
    """A grounded statement the model ties to evidence chunk ids."""

    text: str
    chunk_ids: list[str] = Field(default_factory=list)


class Citation(BaseModel):
    """A verified pointer to a real, in-scope chunk (never fabricated)."""

    chunk_id: str
    document_id: str | None = None
    filename: str | None = None
    page: int | None = None
    section: str | None = None
    #: Character offsets ``(start, end)`` of the claim text inside the chunk,
    #: or ``None`` when the span could not be located.
    span: tuple[int, int] | None = None
    workspace_id: str | None = None
    #: Score in ``[0, 1]`` from the configured claim grounder indicating how
    #: well the chunk substantiates the claim, or ``None`` when no grounder
    #: scored the citation (legacy/existence-only verification).
    grounding_score: float | None = None
    #: Authoritative passage content used for grounding verification.
    #: Populated on every grounder-gated path of ``verify_claims`` with the
    #: exact canonical content the claim was grounded against (the retrieved
    #: chunk text, or the store-hydrated canonical content for graph and
    #: lookup citations), so no grounder-verified citation is left as None
    #: (review1 F6). The legacy existence-only path (``claim_grounder=None``)
    #: skips grounding entirely and may verify a store lookup citation whose
    #: content was never hydrated — the sole documented None case.
    content: str | None = None


class Answer(BaseModel):
    """Final answer: text + verified claims + citations + honest quality/reliability metadata."""

    answer: str
    status: AnswerStatus = AnswerStatus.ANSWERED
    #: Canonical reason code (UPPER_SNAKE_CASE member of
    #: ``core.errors.ReasonCode``, TASK-Q0) when ``status`` is not
    #: ``answered``; ``None`` otherwise. Kept as a plain ``str`` on the wire
    #: for frontend compatibility (``apps/web/src/app/ask/contracts.ts``).
    #: Canonical reason code (UPPER_SNAKE_CASE member of
    #: ``core.errors.ReasonCode``, TASK-Q0) when ``status`` is not
    #: ``answered``; ``None`` otherwise. Kept as a plain ``str`` on the wire
    #: for frontend compatibility (``apps/web/src/app/ask/contracts.ts``).
    reason_code: str | None = None
    reason: str | None = None
    confidence: Confidence = Field(default_factory=Confidence)
    limitations: list[str] = Field(default_factory=list)
    trace_id: str | None = None
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    #: Stage 11 deterministic citation integrity outcomes (T-OR-12).
    citation_integrity: Any | None = None
    #: Stage 12 Gemma semantic verification report (T-OR-12).
    semantic_verification: Any | None = None
    #: Final publication gate evaluation combining Stage 11 and Stage 12 (T-OR-12).
    publication_gate: Any | None = None
    #: Publication gate status: "verified", "needs_review", "abstain", "rejected", or None.
    publication_status: str | None = None
    #: Verifier model identifier that evaluated semantic claims
    #: (e.g. google/gemma-4-26b-a4b-it:free).
    verifier_model: str | None = None


class DocumentStatus(StrEnum):
    """Lifecycle state of the document version a canonical chunk belongs to.

    * ``ACTIVE`` — the current, live version; the chunk is citable.
    * ``DELETED`` — the document was tombstoned (``documents.deleted_at`` set);
      the chunk must never be cited even if a row still exists.
    * ``SUPERSEDED`` — the document version was retired
      (``document_versions.superseded_at`` set); a newer version is live and this
      generation must never be cited.

    Carried on :class:`CanonicalChunk` so Stage 11 can emit a *typed* rejection
    reason (``DELETED_DOCUMENT`` / ``SUPERSEDED_VERSION``) instead of collapsing
    both into a generic miss the way the existing ``ChunkLookup`` (which filters
    deleted/superseded to ``None``) does.
    """

    ACTIVE = "active"
    DELETED = "deleted"
    SUPERSEDED = "superseded"


class CanonicalChunk(BaseModel):
    """Authoritative, store-resolved chunk record (the evidence citations are checked against).

    Stage 11's deterministic integrity validator compares each candidate citation
    against this canonical record rather than against whatever text a retriever
    happened to carry (summaries, payload echoes, synthesized graph descriptions).
    Unlike :class:`Citation`, it carries the document ``status`` (so a deleted or
    superseded source gets a typed rejection reason) and the chunk ``metadata``
    disclosure (``chunk_disclosure`` / ``parse_lost_regions`` / ``extraction_report``)
    so OCR and parsing limitations are propagated to affected citations.

    Additive, backward-compatible: existing call sites and the ``ChunkLookup``
    protocol are unchanged; :class:`~answer.citation_integrity` simply builds
    these records from a ``CanonicalChunk`` mapping or from ``ChunkLookup`` results.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    workspace_id: str | None = None
    document_id: str | None = None
    document_version_id: str | None = None
    filename: str | None = None
    #: Canonical provenance the citation asserts; ``None`` means "not asserted by
    #: the chunk's metadata", which is *not* itself a fabrication.
    page: int | None = None
    section: str | None = None
    #: Original, authoritative passage text (never a summary/payload echo).
    content: str | None = None
    status: DocumentStatus = DocumentStatus.ACTIVE
    #: Disclosure carried by ingestion (see
    #: :func:`apps.worker.jobs.ingest_pipeline.parse_disclosure`): the nested
    #: ``chunk_disclosure`` dict and/or the top-level ``parse_lost_regions`` /
    #: ``extraction_report`` / ``parse_partial`` keys. Read by Stage 11 to flag
    #: unreadable source regions.
    metadata: dict[str, Any] = Field(default_factory=dict)
