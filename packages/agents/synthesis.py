"""Grounded synthesis — every sentence grounded or report doesn't ship (T-V3-020).

Sentence-level grounding gate via production verifier seam (one-grounder rule).
Ungroundable sentences are DROPPED into unknowns, not shipped.
Unknowns + contradictions are carried THROUGH verbatim — no content lost.
Zero groundable content abstains with reason + searched disclosure (T-052 pattern).

CAPS - why bounds matter (V3 budget discipline):
  MAX_SENTENCES=9               # mirrors plan size; final bounded to 9 grounded sentences
  MAX_SENTENCE_CHARS=500        # each sentence truncated; prevents 10KB sentence
  MAX_UNKNOWN_CARRY=9           # unknowns bounded to plan size
  MAX_CONTRADICTION_CARRY=9     # contradictions bounded
  MAX_ABSTENTION_REASON_CHARS=300  # bounded abstention text
  MAX_SEARCHED_CHARS=100        # per searched entry
"""

from __future__ import annotations

from dataclasses import dataclass, field

from answer.verification import ExtractiveGrounder

from .contradictions import Candidate, Contradiction
from .evidence import EvidenceRecord
from .gap import Unknown
from .verifier import ChunkSource, InMemoryChunkSource

# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

MAX_SENTENCES = 9
MAX_SENTENCE_CHARS = 500
MAX_UNKNOWN_CARRY = 9
MAX_CONTRADICTION_CARRY = 9
MAX_ABSTENTION_REASON_CHARS = 300
MAX_SEARCHED_CHARS = 100

ABSTENTION_REASON = "No groundable content: all claims failed grounding or no supporting evidence"


@dataclass(frozen=True)
class GroundedSentence:
    """One grounded final sentence — traces to supporting chunks."""

    sentence: str
    chunk_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    confidence: str = "MEDIUM"
    citations: list[str] = field(default_factory=list)


@dataclass
class SynthesizedReport:
    """Final report — every sentence grounded or explicitly marked."""

    grounded_sentences: list[GroundedSentence] = field(default_factory=list)
    unknowns: list[Unknown] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)
    contradiction_outline: list[str] = field(default_factory=list)
    abstained: bool = False
    abstention_reason: str = ""
    searched: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def has_grounded(self) -> bool:
        return bool(self.grounded_sentences)


class GroundedSynthesizer:
    """Turn verified claims + candidates + unknowns + contradictions into grounded final."""

    def __init__(
        self,
        chunk_source: ChunkSource | InMemoryChunkSource,
        grounder: ExtractiveGrounder | None = None,
    ) -> None:
        self.chunk_source = chunk_source
        self.grounder = grounder or ExtractiveGrounder()

    async def _is_grounded(self, statement: str, chunk_ids: list[str]) -> tuple[bool, list[str]]:
        """True if statement grounded in any chunk."""
        tried: list[str] = []
        for cid in chunk_ids:
            content = await self.chunk_source.get_chunk_content(cid)
            if content is None:
                continue
            tried.append(cid)
            if self.grounder.score(statement, content) >= self.grounder.threshold:
                return True, [cid]
        return False, tried

    async def synthesize(
        self,
        questions: list[str] | None = None,
        verified_claims: list[EvidenceRecord] | None = None,
        candidates: list[Candidate] | None = None,
        gap_unknowns: list[Unknown] | None = None,
        contradictions: list[Contradiction] | None = None,
        contradiction_outline: list[str] | None = None,
        searched: list[str] | None = None,
    ) -> SynthesizedReport:
        verified_claims = verified_claims or []
        candidates = candidates or []
        gap_unknowns = gap_unknowns or []
        contradictions = contradictions or []
        contradiction_outline = contradiction_outline or []
        questions = questions or []
        searched = searched or []

        grounded: list[GroundedSentence] = []
        dropped_unknowns: list[Unknown] = []

        # Build searched disclosure (what was searched — questions + chunk ids)
        all_searched = list(searched)
        for q in questions[:MAX_SENTENCES]:
            if q.strip()[:MAX_SEARCHED_CHARS] not in all_searched:
                all_searched.append(q.strip()[:MAX_SEARCHED_CHARS])

        # Sentence-level grounding gate — verified claims
        for ev in verified_claims[:MAX_SENTENCES]:
            stmt = ev.statement.strip()[:MAX_SENTENCE_CHARS]
            if not stmt:
                continue
            chunk_ids: list[str] = []
            seen: set[str] = set()
            for cid in ev.chunk_ids:
                if cid and cid not in seen:
                    seen.add(cid)
                    chunk_ids.append(cid)
            for c in ev.citations:
                if c.chunk_id not in seen:
                    seen.add(c.chunk_id)
                    chunk_ids.append(c.chunk_id)
            if ev.chunk_id and ev.chunk_id not in seen:
                chunk_ids.append(ev.chunk_id)

            ok, grounded_chunk = await self._is_grounded(stmt, chunk_ids)
            if ok:
                grounded.append(
                    GroundedSentence(
                        sentence=stmt,
                        chunk_ids=grounded_chunk,
                        evidence_ids=[ev.id],
                        confidence="MEDIUM",
                        citations=grounded_chunk,
                    )
                )
            else:
                # DROPPED into unknowns, not shipped
                dropped_unknowns.append(
                    Unknown(
                        question=stmt,
                        searched=chunk_ids,
                        reason="NOT_GROUNDED",
                        tried_chunk_ids=chunk_ids,
                    )
                )

        # Candidates (contradicted but grounded) — also gated, then capped MEDIUM
        for cand in candidates[:MAX_SENTENCES]:
            stmt = cand.statement.strip()[:MAX_SENTENCE_CHARS]
            if not stmt:
                continue
            # Candidates are already grounded via detector, but re-gate
            ok, grounded_chunk = await self._is_grounded(stmt, cand.chunk_ids)
            # Use candidate chunk_ids directly if gate fails but detector said grounded,
            # still re-check — if not grounded now, drop (tombstone case)
            if not ok:
                dropped_unknowns.append(
                    Unknown(
                        question=cand.question,
                        searched=cand.chunk_ids,
                        reason="NOT_GROUNDED",
                        tried_chunk_ids=cand.chunk_ids,
                    )
                )
                continue
            grounded.append(
                GroundedSentence(
                    sentence=stmt,
                    chunk_ids=grounded_chunk or cand.chunk_ids,
                    evidence_ids=[cand.evidence_id],
                    confidence="MEDIUM",
                    citations=grounded_chunk or cand.chunk_ids,
                )
            )

        # Deduplicate grounded sentences by sentence text (keep first)
        seen_s: set[str] = set()
        deduped: list[GroundedSentence] = []
        for g in grounded:
            if g.sentence.lower() not in seen_s:
                seen_s.add(g.sentence.lower())
                deduped.append(g)
            if len(deduped) >= MAX_SENTENCES:
                break

        # Unknowns + contradictions carried THROUGH verbatim — no content lost
        final_unknowns = list(gap_unknowns[:MAX_UNKNOWN_CARRY]) + dropped_unknowns[:MAX_UNKNOWN_CARRY]  # noqa: E501
        final_unknowns = final_unknowns[:MAX_UNKNOWN_CARRY]
        final_contradictions = list(contradictions[:MAX_CONTRADICTION_CARRY])
        final_outline = list(contradiction_outline[:MAX_CONTRADICTION_CARRY])

        # Abstention honesty — zero groundable content abstains with reason + searched
        abstained = False
        abstention_reason = ""
        limitations: list[str] = []
        if not deduped:
            abstained = True
            abstention_reason = ABSTENTION_REASON[:MAX_ABSTENTION_REASON_CHARS]
            limitations.append(f"Searched: {', '.join(all_searched[:5])}" if all_searched else "Searched: no queries")  # noqa: E501

        # If contradicted, add limitation (T-052 pattern: capped already, now disclose)
        if final_contradictions:
            limitations.append(f"Contradictions present: {len(final_contradictions)} pair(s) — confidence capped to MEDIUM")  # noqa: E501

        return SynthesizedReport(
            grounded_sentences=deduped,
            unknowns=final_unknowns,
            contradictions=final_contradictions,
            contradiction_outline=final_outline,
            abstained=abstained,
            abstention_reason=abstention_reason,
            searched=all_searched[:MAX_SENTENCES],
            limitations=limitations,
        )
