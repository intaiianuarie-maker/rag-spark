"""Gap analysis + unknowns retention (T-V3-018).

After evidence gathering, compute the GAP — which plan questions have no
supporting evidence — and RETAIN unknowns as first-class output.

One-grounder rule: reuses ExtractiveGrounder from answer.verification.
Never fabricates a gap-filler; plausible-but-unrelated chunks are rejected.

CAPS - why bounds matter (V3 budget discipline):
  MAX_QUESTIONS=9               # mirrors planner/fanout; gap never exceeds plan size
  MAX_UNKNOWN_REASON_CHARS=200  # bounded reason text
  MAX_UNKNOWN_QUESTION_CHARS=200# inherited from planner question cap

An Unknown is first-class: question, what was searched (chunk_ids/queries),
and why nothing qualified (NO_EVIDENCE, NOT_GROUNDED, CHUNK_MISSING).
Zero supporting chunks -> named unknown, not a hole.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from answer.verification import ExtractiveGrounder

from .evidence import EvidenceRecord
from .verifier import ChunkSource, InMemoryChunkSource

# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

MAX_QUESTIONS = 9
MAX_UNKNOWN_REASON_CHARS = 200
MAX_UNKNOWN_QUESTION_CHARS = 200

# Reasons — visible, never silent
REASON_NO_EVIDENCE = "NO_EVIDENCE"
REASON_NOT_GROUNDED = "NOT_GROUNDED"
REASON_CHUNK_MISSING = "CHUNK_MISSING"


@dataclass(frozen=True)
class Unknown:
    """One retained unknown — question, what was searched, why nothing qualified."""

    question: str
    searched: list[str] = field(default_factory=list)
    reason: str = REASON_NO_EVIDENCE
    tried_chunk_ids: list[str] = field(default_factory=list)


@dataclass
class GapReport:
    """Gap analysis result — unknowns are first-class, never silent omission."""

    unknowns: list[Unknown] = field(default_factory=list)
    covered: list[str] = field(default_factory=list)
    total_questions: int = 0

    @property
    def gaps(self) -> int:
        return len(self.unknowns)


class GapAnalyzer:
    """Compute gaps via production grounder — no fabricated fillers."""

    def __init__(
        self,
        chunk_source: ChunkSource | InMemoryChunkSource,
        grounder: ExtractiveGrounder | None = None,
    ) -> None:
        self.chunk_source = chunk_source
        self.grounder = grounder or ExtractiveGrounder()

    async def _is_evidence_grounded(self, ev: EvidenceRecord) -> tuple[bool, str]:
        """True if evidence statement is grounded in any of its chunks."""
        ids: list[str] = []
        seen: set[str] = set()
        for cid in ev.chunk_ids:
            if cid and cid not in seen:
                seen.add(cid)
                ids.append(cid)
        for c in ev.citations:
            if c.chunk_id and c.chunk_id not in seen:
                seen.add(c.chunk_id)
                ids.append(c.chunk_id)
        if ev.chunk_id and ev.chunk_id not in seen:
            ids.append(ev.chunk_id)
        if not ids:
            return False, REASON_NO_EVIDENCE
        missing = 0
        for cid in ids:
            content = await self.chunk_source.get_chunk_content(cid)
            if content is None:
                missing += 1
                continue
            score = self.grounder.score(ev.statement, content)
            if score >= self.grounder.threshold:
                return True, "GROUNDED"
        if missing == len(ids):
            return False, REASON_CHUNK_MISSING
        return False, REASON_NOT_GROUNDED

    async def analyze(
        self,
        questions: list[str],
        evidence_by_question: dict[str, list[EvidenceRecord]] | None = None,
        searched_by_question: dict[str, list[str]] | None = None,
    ) -> GapReport:
        # Truncate to plan size
        qs = [q.strip()[:MAX_UNKNOWN_QUESTION_CHARS] for q in questions[:MAX_QUESTIONS] if q.strip()]  # noqa: E501
        evidence_by_question = evidence_by_question or {}
        searched_by_question = searched_by_question or {}

        unknowns: list[Unknown] = []
        covered: list[str] = []

        for q in questions[:MAX_QUESTIONS]:
            if not q.strip():
                continue
            q_key = q.strip()[:MAX_UNKNOWN_QUESTION_CHARS]
            evs = evidence_by_question.get(q, []) or evidence_by_question.get(q_key, [])
            searched = searched_by_question.get(q, searched_by_question.get(q_key, [])) or []
            # Also collect chunk ids from evidence as searched
            tried_ids: list[str] = list(searched)
            for ev in evs:
                for cid in ev.chunk_ids:
                    if cid not in tried_ids:
                        tried_ids.append(cid)
                for c in ev.citations:
                    if c.chunk_id not in tried_ids:
                        tried_ids.append(c.chunk_id)
                if ev.chunk_id and ev.chunk_id not in tried_ids:
                    tried_ids.append(ev.chunk_id)

            if not evs:
                unknowns.append(Unknown(question=q_key, searched=searched, reason=REASON_NO_EVIDENCE, tried_chunk_ids=tried_ids))  # noqa: E501
                continue

            # Check if any evidence for this question is grounded
            grounded_any = False
            last_reason = REASON_NOT_GROUNDED
            for ev in evs:
                ok, reason = await self._is_evidence_grounded(ev)
                if ok:
                    grounded_any = True
                    break
                last_reason = reason

            if grounded_any:
                covered.append(q_key)
            else:
                # All plausible-but-unrelated rejected -> retain unknown, never filler
                reason = last_reason[:MAX_UNKNOWN_REASON_CHARS]
                unknowns.append(Unknown(question=q_key, searched=searched, reason=reason, tried_chunk_ids=tried_ids))  # noqa: E501

        return GapReport(unknowns=unknowns, covered=covered, total_questions=len(qs))

    async def analyze_flat(
        self,
        questions: list[str],
        evidence: list[EvidenceRecord],
    ) -> GapReport:
        """Flat evidence list — each evidence tried for each question (adversarial check)."""
        # For adversarial test: if retriever returned same unrelated chunks for a question,
        # flat check will still require grounding per question, so unrelated remains gap.
        by_q: dict[str, list[EvidenceRecord]] = {q: list(evidence) for q in questions}
        return await self.analyze(questions, by_q)
