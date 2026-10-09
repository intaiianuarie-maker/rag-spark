"""Evidence Store implementation (Section 29).

Persists verified claims, quotes, sources, and researchers; provides
contradiction detection between evidence items.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from .interfaces import Contradiction, ResearchEvidence


class EvidenceStore(ABC):
    """Abstract storage for research evidence items."""

    @abstractmethod
    async def add_evidence(self, workspace_id: str, items: Sequence[ResearchEvidence]) -> int:
        """Add research evidence items to the store."""
        raise NotImplementedError

    @abstractmethod
    async def get_evidence(self, workspace_id: str, evidence_id: str) -> ResearchEvidence | None:
        """Fetch a specific evidence item."""
        raise NotImplementedError

    @abstractmethod
    async def list_evidence(
        self, workspace_id: str, query_id: str | None = None
    ) -> list[ResearchEvidence]:
        """List evidence for a workspace, optionally filtered by query/topic."""
        raise NotImplementedError

    @abstractmethod
    async def detect_contradictions(self, workspace_id: str) -> list[Contradiction]:
        """Identify contradicting claims in the accumulated evidence."""
        raise NotImplementedError


class MemoryEvidenceStore(EvidenceStore):
    """In-memory EvidenceStore with workspace isolation."""

    def __init__(self) -> None:
        # workspace_id -> evidence_id -> ResearchEvidence
        self._store: dict[str, dict[str, ResearchEvidence]] = {}

    def _ensure_ws(self, ws: str) -> None:
        if ws not in self._store:
            self._store[ws] = {}

    async def add_evidence(self, workspace_id: str, items: Sequence[ResearchEvidence]) -> int:
        self._ensure_ws(workspace_id)
        written = 0
        for item in items:
            if item.workspace_id == workspace_id:
                self._store[workspace_id][item.id] = item
                written += 1
        return written

    async def get_evidence(self, workspace_id: str, evidence_id: str) -> ResearchEvidence | None:
        self._ensure_ws(workspace_id)
        return self._store[workspace_id].get(evidence_id)

    async def list_evidence(
        self, workspace_id: str, query_id: str | None = None
    ) -> list[ResearchEvidence]:
        self._ensure_ws(workspace_id)
        all_items = list(self._store[workspace_id].values())
        if query_id:
            return [it for it in all_items if it.query_id == query_id]
        return all_items

    async def detect_contradictions(self, workspace_id: str) -> list[Contradiction]:
        """Rule-based and tag-based contradiction detector."""
        self._ensure_ws(workspace_id)
        items = list(self._store[workspace_id].values())
        contradictions: list[Contradiction] = []

        # Simple semantic contradiction check (opposing polarity or conflict tags)
        for i, a in enumerate(items):
            for b in items[i + 1 :]:
                # Check explicit negation keywords on common subjects
                common_tags = set(a.tags) & set(b.tags)
                if not common_tags and a.query_id != b.query_id:
                    continue

                words_a = set(a.claim.lower().split())
                words_b = set(b.claim.lower().split())

                # If discussing same entity but one asserts positive and other negative
                has_neg_a = any(neg in words_a for neg in ("not", "never", "cannot", "no"))
                has_neg_b = any(neg in words_b for neg in ("not", "never", "cannot", "no"))

                if has_neg_a != has_neg_b:
                    overlap = (words_a - {"not", "never", "cannot", "no"}) & (
                        words_b - {"not", "never", "cannot", "no"}
                    )
                    if len(overlap) >= 3:
                        contradictions.append(
                            Contradiction(
                                claim_a=a.claim,
                                claim_b=b.claim,
                                evidence_a_id=a.id,
                                evidence_b_id=b.id,
                                reason=(
                                    f"Polarity conflict on: {', '.join(sorted(overlap)[:3])}"
                                ),
                            )
                        )

        return contradictions
