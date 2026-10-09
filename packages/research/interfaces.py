"""Research Engine & Evidence Store interfaces (Phase 12 / Sections 28-29).

Decouples deep research from single-prompt generation through a durable EvidenceStore,
subquestion decomposition, gap/contradiction analysis, and structured reporting.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ResearchStatus(StrEnum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"


class ResearchEvidence(BaseModel):
    """An individual verified evidence unit supporting a research finding."""

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


class ResearchReport(BaseModel):
    """A structured, source-backed final research output."""

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
