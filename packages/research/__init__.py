"""Research Engine and Evidence Store package (Phase 12 / Sections 28-29)."""

from .engine import ResearchEngine
from .evidence_store import EvidenceStore, MemoryEvidenceStore
from .interfaces import (
    Contradiction,
    ResearchEvidence,
    ResearchPlan,
    ResearchReport,
    ResearchStatus,
)

__all__ = [
    "Contradiction",
    "EvidenceStore",
    "MemoryEvidenceStore",
    "ResearchEngine",
    "ResearchEvidence",
    "ResearchPlan",
    "ResearchReport",
    "ResearchStatus",
]
