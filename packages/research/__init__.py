"""Research Engine and Evidence Store package (Phase 12 / Sections 28-29) + V3 contracts (A0)."""

from .engine import (
    ResearchEngine,
    ResearchEngineError,
    ResearchModelPolicy,
)
from .evidence_store import EvidenceStore, EvidenceStoreError, MemoryEvidenceStore
from .interfaces import (
    Citation,
    ClaimStatus,
    Contradiction,
    EvidenceVerificationState,
    ReportClaim,
    ResearchEvidence,
    ResearchPlan,
    ResearchReport,
    ResearchRun,
    ResearchStatus,
)

__all__ = [
    "Citation",
    "ClaimStatus",
    "Contradiction",
    "EvidenceStore",
    "EvidenceStoreError",
    "EvidenceVerificationState",
    "MemoryEvidenceStore",
    "ReportClaim",
    "ResearchEngine",
    "ResearchEngineError",
    "ResearchEvidence",
    "ResearchModelPolicy",
    "ResearchPlan",
    "ResearchReport",
    "ResearchRun",
    "ResearchStatus",
]
