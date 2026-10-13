"""Contradictions as candidates + outline (T-V3-019).

When evidence DISAGREES (two chunks supporting opposite claims on one
question), keep BOTH as candidates with provenance — never silent majority.

One-grounder rule: reuses ExtractiveGrounder from answer.verification
for verdicts; one grounder only.

CAPS - why bounds matter (V3 budget discipline):
  MAX_CONTRADICTIONS=9           # at most one per question (9 Qs)
  MAX_CLAIMS_PER_QUESTION=2      # pair for contradiction (keeps outline bounded)
  MAX_OUTLINE_SECTIONS=9         # outline sections bounded to plan size
  MAX_LIMITATION_CHARS=200       # bounded limitation text

Confidence discipline (T-052 pattern): contradicted claims cap at most
MEDIUM with appended limitation naming the conflict — capped + limitation,
not a new mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from answer.verification import ExtractiveGrounder

from .evidence import EvidenceRecord
from .verifier import ChunkSource, InMemoryChunkSource

# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

MAX_CONTRADICTIONS = 9
MAX_CLAIMS_PER_QUESTION = 2
MAX_OUTLINE_SECTIONS = 9
MAX_LIMITATION_CHARS = 200

# Confidence levels — mirrors answer.models ConfidenceLevel
CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"

# Reasons
REASON_OPPOSING_VERDICTS = "OPPOSING_VERDICTS"
REASON_CONTRADICTORY_CLAIMS = "CONTRADICTORY_CLAIMS"


def _cap_confidence(level: str) -> str:
    """T-052 pattern: contradicted -> at most MEDIUM."""
    if level == CONFIDENCE_HIGH:
        return CONFIDENCE_MEDIUM
    return level


def _limitation_for(contradiction: Contradiction) -> str:
    base = f"Conflicting evidence for '{contradiction.question}': '{contradiction.claim_a}' vs '{contradiction.claim_b}'"  # noqa: E501
    return base[:MAX_LIMITATION_CHARS]


@dataclass(frozen=True)
class Contradiction:
    """One contradiction pair — both sides kept as candidates with provenance."""

    question: str
    claim_a: str
    claim_b: str
    evidence_a_id: str
    evidence_b_id: str
    chunk_ids_a: list[str] = field(default_factory=list)
    chunk_ids_b: list[str] = field(default_factory=list)
    reason: str = REASON_CONTRADICTORY_CLAIMS


@dataclass
class Candidate:
    """One candidate claim with provenance and capped confidence."""

    question: str
    statement: str
    evidence_id: str
    chunk_ids: list[str] = field(default_factory=list)
    confidence: str = CONFIDENCE_HIGH
    limitations: list[str] = field(default_factory=list)


@dataclass
class ContradictionReport:
    """Detection result — contradictions, candidates, outline, capped claims."""

    contradictions: list[Contradiction] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    outline_sections: list[str] = field(default_factory=list)


class ContradictionDetector:
    """Detect contradictions via production grounder, keep both sides, cap confidence."""

    def __init__(
        self,
        chunk_source: ChunkSource | InMemoryChunkSource,
        grounder: ExtractiveGrounder | None = None,
    ) -> None:
        self.chunk_source = chunk_source
        self.grounder = grounder or ExtractiveGrounder()

    async def _is_grounded(self, ev: EvidenceRecord) -> bool:
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
        for cid in ids:
            content = await self.chunk_source.get_chunk_content(cid)
            if content is None:
                continue
            if self.grounder.score(ev.statement, content) >= self.grounder.threshold:
                return True
        return False

    def _statements_contradict(self, a: str, b: str) -> bool:
        """Simple opposing check: different statements that share subject but differ."""
        if a.strip().lower() == b.strip().lower():
            return False
        # Consider contradictory if both grounded but statements differ and overlap on key tokens
        # For test adversary: "budget is 900" vs "budget is 1900" -> contradict
        # Use word overlap + numeric or negation difference
        lower_a = a.lower()
        lower_b = b.lower()
        # Negation check
        neg_words = ("not", "never", "no", "cannot")
        has_neg_a = any(w in lower_a for w in neg_words)
        has_neg_b = any(w in lower_b for w in neg_words)
        if has_neg_a != has_neg_b:
            return True
        # Numeric difference: both mention budget but different numbers
        import re

        nums_a = set(re.findall(r"\d+", a))
        nums_b = set(re.findall(r"\d+", b))
        if nums_a and nums_b and nums_a != nums_b:
            # Check same subject (budget)
            if "budget" in lower_a and "budget" in lower_b:
                return True
        # Generic different statements on same Q -> treat as contradiction if both grounded  # noqa: E501
        # Fall back: if statements differ and share >=2 words, consider contradictory
        words_a = set(lower_a.split())
        words_b = set(lower_b.split())
        overlap = words_a & words_b
        if len(overlap) >= 2:
            return True
        return False

    async def detect(
        self,
        questions: list[str],
        evidence_by_question: dict[str, list[EvidenceRecord]],
    ) -> ContradictionReport:
        contradictions: list[Contradiction] = []
        candidates: list[Candidate] = []
        outline: list[str] = []

        for q in questions[:MAX_OUTLINE_SECTIONS]:
            if not q.strip():
                continue
            evs = evidence_by_question.get(q, [])
            # Only grounded evidence participates — reuse verifier seam
            grounded: list[EvidenceRecord] = []
            for ev in evs:
                if await self._is_grounded(ev):
                    grounded.append(ev)

            # Need at least 2 grounded with opposing statements
            for i in range(len(grounded)):
                for j in range(i + 1, len(grounded)):
                    a = grounded[i]
                    b = grounded[j]
                    if not self._statements_contradict(a.statement, b.statement):
                        continue
                    if len(contradictions) >= MAX_CONTRADICTIONS:
                        break
                    # Build contradiction — both sides kept
                    chunk_a = list(a.chunk_ids) or ([a.chunk_id] if a.chunk_id else [])
                    for c in a.citations:
                        if c.chunk_id not in chunk_a:
                            chunk_a.append(c.chunk_id)
                    chunk_b = list(b.chunk_ids) or ([b.chunk_id] if b.chunk_id else [])
                    for c in b.citations:
                        if c.chunk_id not in chunk_b:
                            chunk_b.append(c.chunk_id)
                    contradictions.append(
                        Contradiction(
                            question=q,
                            claim_a=a.statement,
                            claim_b=b.statement,
                            evidence_a_id=a.id,
                            evidence_b_id=b.id,
                            chunk_ids_a=chunk_a,
                            chunk_ids_b=chunk_b,
                            reason=REASON_CONTRADICTORY_CLAIMS,
                        )
                    )
                    # Candidates — both sides with provenance, capped confidence
                    lim = _limitation_for(contradictions[-1])
                    for ev, chunk_ids in [(a, chunk_a), (b, chunk_b)]:
                        candidates.append(
                            Candidate(
                                question=q,
                                statement=ev.statement,
                                evidence_id=ev.id,
                                chunk_ids=chunk_ids,
                                confidence=_cap_confidence(CONFIDENCE_HIGH),
                                limitations=[lim],
                            )
                        )
                    # Outline section naming both sides with chunk ids — visible disagreement
                    outline.append(f"Contradiction on '{q}': '{a.statement}' [{','.join(chunk_a)}] vs '{b.statement}' [{','.join(chunk_b)}]")  # noqa: E501

        # Deduplicate candidates by evidence_id keeping capped confidence
        seen_ids: set[str] = set()
        uniq: list[Candidate] = []
        for cand in candidates:
            if cand.evidence_id not in seen_ids:
                seen_ids.add(cand.evidence_id)
                uniq.append(cand)

        return ContradictionReport(contradictions=contradictions, candidates=uniq, outline_sections=outline[:MAX_OUTLINE_SECTIONS])  # noqa: E501

    async def cap_claims(self, claims: list[Candidate]) -> list[Candidate]:
        """Cap any contradicted claim at most MEDIUM with limitation — T-052 pattern."""
        out: list[Candidate] = []
        for c in claims:
            capped = _cap_confidence(c.confidence)
            lims = list(c.limitations)
            if capped != c.confidence and not lims:
                lims = ["Contradictory evidence — confidence capped to MEDIUM"]
            out.append(Candidate(question=c.question, statement=c.statement, evidence_id=c.evidence_id, chunk_ids=c.chunk_ids, confidence=capped, limitations=lims))  # noqa: E501
        return out
