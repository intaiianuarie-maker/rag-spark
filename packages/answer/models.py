"""Answer engine data models (TASK-M4-002).

The structured LLM output is ``{answer, claims[{text, chunk_ids}]}``; the
verified, citation-ready output is :class:`Answer`. Citations carry the
provenance the UI needs to open the exact passage (doc, filename, page,
section, span).
"""

from __future__ import annotations

from enum import StrEnum

from core.errors import (
    AnswerStatus as AnswerStatus,  # re-export: single shared definition (T-V1-025)
)
from pydantic import BaseModel, Field


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
