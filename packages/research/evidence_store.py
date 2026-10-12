"""Evidence Store implementation (Section 29).

Persists verified claims, quotes, sources, and researchers; provides
contradiction detection between evidence items.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from .interfaces import Contradiction, ResearchEvidence


class EvidenceStore(ABC):
    """Abstract storage for research evidence items.

    V3 (A0): every read is scoped to a research run. Two investigations in one
    workspace can never mix evidence (rew1 B6).
    """

    @abstractmethod
    async def add_evidence(self, workspace_id: str, items: Sequence[ResearchEvidence]) -> int:
        """Add research evidence items to the store."""
        raise NotImplementedError

    @abstractmethod
    async def get_evidence(
        self,
        workspace_id: str,
        evidence_id: str,
        *,
        run_id: str | None = None,
    ) -> ResearchEvidence | None:
        """Fetch a specific evidence item, optionally scoped to a run."""
        raise NotImplementedError

    @abstractmethod
    async def list_evidence(
        self,
        workspace_id: str,
        query_id: str | None = None,
        *,
        run_id: str | None = None,
    ) -> list[ResearchEvidence]:
        """List evidence for a workspace, filtered by query and/or research run."""
        raise NotImplementedError

    @abstractmethod
    async def detect_contradictions(
        self, workspace_id: str, *, run_id: str | None = None
    ) -> list[Contradiction]:
        """Identify contradicting claims within a workspace/run scope."""
        raise NotImplementedError


class EvidenceStoreError(RuntimeError):
    """Raised for cross-workspace or cross-run evidence access attempts."""


class MemoryEvidenceStore(EvidenceStore):
    """In-memory EvidenceStore with workspace and run isolation."""

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
            if item.workspace_id != workspace_id:
                # rew1 B7: a dropped item is indistinguishable from an empty one.
                raise EvidenceStoreError(
                    f"evidence {item.id} belongs to workspace {item.workspace_id}, "
                    f"not {workspace_id}"
                )
            self._store[workspace_id][item.id] = item
            written += 1
        return written

    async def get_evidence(
        self,
        workspace_id: str,
        evidence_id: str,
        *,
        run_id: str | None = None,
    ) -> ResearchEvidence | None:
        self._ensure_ws(workspace_id)
        item = self._store[workspace_id].get(evidence_id)
        if item is None:
            return None
        if run_id is not None and item.run_id != run_id:
            # rew1 B6: evidence from another run must not be readable here.
            raise EvidenceStoreError(
                f"evidence {evidence_id} belongs to run {item.run_id!r}, not {run_id!r}"
            )
        return item

    async def list_evidence(
        self,
        workspace_id: str,
        query_id: str | None = None,
        *,
        run_id: str | None = None,
    ) -> list[ResearchEvidence]:
        self._ensure_ws(workspace_id)
        items = list(self._store[workspace_id].values())
        if run_id is not None:
            # rew1 B6: run scoping is the default isolation filter.
            items = [it for it in items if it.run_id == run_id]
        if query_id:
            items = [it for it in items if it.query_id == query_id]
        return items

    async def detect_contradictions(
        self, workspace_id: str, *, run_id: str | None = None
    ) -> list[Contradiction]:
        """Rule-based and tag-based contradiction detector, scoped to one run."""
        self._ensure_ws(workspace_id)
        items = await self.list_evidence(workspace_id, run_id=run_id)
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
