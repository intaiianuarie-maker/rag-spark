"""Research Engine & Evidence Store interfaces (Phase 12 / Sections 28-29).

Decouples deep research from single-prompt generation through a durable EvidenceStore,
subquestion decomposition, gap/contradiction analysis, and structured reporting.

V3 (card A0) adds the product evidence contracts: verification state, original-source
provenance (document version/chunk, quote span, source hash), research-run scoping and
:class:`ReportClaim`. These are the seams the persistent evidence adapter (R0) and the
STORM-backed planner (R1) implement.

Prototype note: :class:`ResearchEvidence` gains new optional fields so the prototype
engine and its tests keep working; the durable adapter (R0) fills them all.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class ResearchStatus(StrEnum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"


class EvidenceVerificationState(StrEnum):
    """Verification lifecycle for one evidence unit.

    A caller-supplied claim is ``UNVERIFIED`` until an original-source check
    (document version + chunk + quote span) marks it ``VERIFIED``. Heuristic
    conflicts are ``DISPUTED`` candidates, never verdicts. ``REVOKED`` /
    ``DELETED`` mean the source is gone and citations must be invalidated.
    """

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    DISPUTED = "disputed"
    REVOKED = "revoked"
    DELETED = "deleted"


class ResearchEvidence(BaseModel):
    """An individual evidence unit supporting a research finding.

    V3 fields (all optional for prototype compatibility): ``run_id`` /
    ``subquestion_id`` scope the unit to one research run so two investigations
    in one workspace can never mix evidence; ``document_id`` / ``document_version``
    / ``chunk_id`` / ``quote_start`` / ``quote_end`` / ``source_hash`` pin the
    original source; ``verification`` + ``verification_reason`` record how the
    claim was checked; ``revoked_at`` / ``deleted_at`` record source loss.
    """

    id: str
    workspace_id: str
    query_id: str = ""
    claim: str
    source: str
    quote: str
    confidence: float = 1.0
    researcher: str = "agent"
    tags: list[str] = Field(default_factory=list)
    chunk_ids: list[str] = Field(default_factory=list)
    timestamp: str = ""
    # V3 (A0) original-source provenance + verification state
    run_id: str = ""
    subquestion_id: str = ""
    document_id: str = ""
    document_version: str = ""
    chunk_id: str = ""
    quote_start: int | None = None
    quote_end: int | None = None
    source_hash: str = ""
    verification: EvidenceVerificationState = EvidenceVerificationState.UNVERIFIED
    verification_reason: str = ""
    revoked_at: str | None = None
    deleted_at: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> ResearchEvidence:
        if self.quote_start is not None and self.quote_end is not None:
            if self.quote_start < 0 or self.quote_end < self.quote_start:
                raise ValueError("quote span must be a valid non-negative range")
        return self


class Contradiction(BaseModel):
    """A detected conflict between two research evidence claims."""

    claim_a: str
    claim_b: str
    evidence_a_id: str
    evidence_b_id: str
    reason: str


class ResearchPlan(BaseModel):
    """Decomposition of a research topic into subquestions."""

    topic: str
    subquestions: list[str] = Field(default_factory=list)
    scope: str = ""


class Citation(BaseModel):
    """Original-source citation resolved to an exact stored chunk span.

    A citation is only valid when it resolves to a real stored chunk with valid
    provenance (document, page, section, span) in the caller's workspace.
    Fabricated citations are rejected (AGENTS.md rule 4).
    """

    document_id: str
    chunk_id: str
    page: int | None = None
    section: str = ""
    span_start: int | None = None
    span_end: int | None = None
    quote: str = ""
    source_hash: str = ""


class ClaimStatus(StrEnum):
    """Support status for one report claim."""

    SUPPORTED = "supported"
    DISPUTED = "disputed"
    UNKNOWN = "unknown"


class ReportClaim(BaseModel):
    """A statement tied to the evidence IDs that support it.

    ``SUPPORTED`` requires at least one evidence id (schema-enforced, rew1 B8);
    a caller-supplied claim with no verified evidence is ``UNKNOWN`` at best,
    never ``SUPPORTED``. Citations resolve to original sources, not to model
    prose.
    """

    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    status: ClaimStatus = ClaimStatus.UNKNOWN

    @model_validator(mode="after")
    def _validate_supported(self) -> ReportClaim:
        if self.status is ClaimStatus.SUPPORTED and not self.evidence_ids:
            raise ValueError("SUPPORTED claims must reference at least one evidence id")
        if not self.statement.strip():
            raise ValueError("statement must be non-empty")
        for citation in self.citations:
            if not citation.chunk_id:
                raise ValueError("citations must resolve to a real chunk")
        return self


class ResearchRun(BaseModel):
    """A scoped research investigation inside one workspace.

    Evidence is scoped to ``run_id`` so two investigations in one workspace
    can never mix evidence (V3 plan invariant 1).
    """

    run_id: str
    workspace_id: str
    topic: str
    status: ResearchStatus = ResearchStatus.PLANNED
    created_at: str = ""
    completed_at: str | None = None


class ResearchReport(BaseModel):
    """A structured, source-backed final research output.

    V3 (A0): ``claims`` separates supported findings, disputed candidates,
    recommendations and unknowns; ``supported_findings`` /
    ``disputed_candidates`` are the typed views over ``claims``. A report with
    no verified evidence abstains — it never claims completed successful
    research (V3 plan invariant 6).
    """

    id: str
    workspace_id: str
    topic: str
    status: ResearchStatus = ResearchStatus.PLANNED
    executive_summary: str = ""
    findings: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    created_at: str = ""
    completed_at: str | None = None
    # V3 (A0) typed claim structure
    claims: list[ReportClaim] = Field(default_factory=list)
    supported_findings: list[str] = Field(default_factory=list)
    disputed_candidates: list[str] = Field(default_factory=list)
