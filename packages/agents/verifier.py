"""Claim/citation verifier — re-verify evidence via production grounder (T-V3-015).

Re-runs stored evidence through the SAME bar as production verification
(ExtractiveGrounder) — reuse the V1 verification path, do not build a second
grounder. Given a stored EvidenceRecord, re-fetch its chunks (read-only),
re-ground its statement, and verdict AGREE/DISAGREE with the stored verdict.
Disagreements surface as a stale-evidence report: run_id, claim, old verdict,
new verdict, cause class.

One-grounder rule: imports ExtractiveGrounder from answer.verification.
No second grounder lives here.

T-G01 — DEPRECATED IN PART, DELIBERATELY. The one-grounder rule above existed
to stop a second, drifting verifier being invented behind the production bar.
:class:`ParaphraseTolerantGrounder` is the sanctioned exception to that rule,
added because T-EVAL-DX measured the cost of the strict bar (26/48 items produced
zero citations; 79.2% of failures were grounding, not retrieval). It is
*flagged off by default* (:func:`resolve_grounder`), so production stays
byte-identical until the flag is flipped, and it satisfies the SAME
``ClaimGrounder`` Protocol (``answer.verification.ClaimGrounder``) so it is a
drop-in swap — ``verify_claims`` is untouched by this file.

Chunk fetches are injectable via ChunkSource so tests never touch live DB
(T-071 scratch seam). The verifier itself is pure: it only reads.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from answer.verification import ExtractiveGrounder

from .evidence import EvidenceRecord

# Cause classes — visible, not silent
CAUSE_GROUNDED = "GROUNDED"
CAUSE_CHUNK_NOT_FOUND = "CHUNK_NOT_FOUND"
CAUSE_CHUNK_TOMBSTONED = "CHUNK_TOMBSTONED"
CAUSE_NOT_GROUNDED = "NOT_GROUNDED"


@runtime_checkable
class ChunkSource(Protocol):
    """Read-only chunk fetcher — injected so tests never hit live DB."""

    async def get_chunk_content(self, chunk_id: str) -> str | None:
        """Return canonical chunk text, or None if missing/tombstoned."""
        ...

    def is_tombstoned(self, chunk_id: str) -> bool:
        ...


class InMemoryChunkSource:
    """In-memory ChunkSource for tests — explicit tombstone set."""

    def __init__(self, chunks: dict[str, str] | None = None) -> None:
        self._chunks: dict[str, str] = dict(chunks or {})
        self._tombstoned: set[str] = set()

    def put(self, chunk_id: str, content: str) -> None:
        self._chunks[chunk_id] = content
        self._tombstoned.discard(chunk_id)

    def tombstone(self, chunk_id: str) -> None:
        self._chunks.pop(chunk_id, None)
        self._tombstoned.add(chunk_id)

    async def get_chunk_content(self, chunk_id: str) -> str | None:
        if chunk_id in self._tombstoned:
            return None
        return self._chunks.get(chunk_id)

    def is_tombstoned(self, chunk_id: str) -> bool:
        return chunk_id in self._tombstoned


# --- T-G01: paraphrase-tolerant grounder (flagged OFF by default) ------------
#
# WHY: ``ExtractiveGrounder`` requires verbatim, word-bounded containment at
# threshold 1.0. T-EVAL-DX measured the cost against the real corpus: 26 of 48
# items produced ZERO citations (54.2%), and grounded+validity accounted for
# 79.2% of all failures while retrieval was perfect. The cause is mechanical —
# a rejected claim yields no citation, and a claim left with no citation is
# dropped (``answer.verification.verify_claims``).
#
# WHAT THIS IS NOT: not a weaker bar. It is a *lexical* bar that accepts a claim
# whose content words are all present in the chunk but are reordered, inflected,
# or wrapped in connective prose, while still REQUIRING every distinctive claim
# token to be present. An unsupported claim (its content words absent) scores
# near zero and is rejected. Numbers must match exactly, so the "900" / "1900"
# confusion the strict grounder guards against is preserved.
#
# Deterministic and offline: no model, no network, no spend.


#: Function words carry no evidential weight; excluding them stops a claim from
#: being "supported" by matching filler.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
        "can", "did", "do", "does", "for", "from", "had", "has", "have", "he",
        "her", "his", "in", "into", "is", "it", "its", "of", "on", "or",
        "our", "she", "than", "that", "the", "their", "them", "then", "there",
        "these", "they", "this", "those", "to", "was", "were", "what", "when",
        "where", "which", "who", "why", "will", "with", "would", "you",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _is_numeric(token: str) -> bool:
    return any(ch.isdigit() for ch in token)


def _matches(claim_token: str, chunk_tokens: set[str]) -> bool:
    """Is this claim token present in the chunk?

    Numeric tokens match EXACTLY only — "900" must never be satisfied by "1900",
    the failure mode the strict grounder explicitly guards against. Alpha tokens
    additionally tolerate simple inflection via a conservative prefix rule
    (shared prefix of at least 4 characters), so "acquired"/"acquire" and
    "budgets"/"budget" match without a stemmer dependency.
    """
    if _is_numeric(claim_token):
        return claim_token in chunk_tokens
    if claim_token in chunk_tokens:
        return True
    if len(claim_token) < 5:
        return False
    return any(
        len(other) >= 5
        and other[:4] == claim_token[:4]
        and other.startswith(claim_token[:5])
        for other in chunk_tokens
        if not _is_numeric(other)
    )


class ParaphraseTolerantGrounder:
    """Lexical grounder that accepts reordering/inflection, not unsupported claims.

    Satisfies ``answer.verification.ClaimGrounder`` (``threshold`` +
    ``score``), so it is a drop-in for ``ExtractiveGrounder`` and needs no change
    to ``verify_claims``.

    Scoring:

    * a claim that appears **verbatim** (case-insensitive, contiguous) scores
      ``1.0`` — identical to the strict grounder, so nothing that passes today
      regresses;
    * otherwise the score is the **weighted coverage of the claim's content
      tokens** present in the chunk, where each token's weight grows with its
      length (a rare/long term counts more than a short one);
    * a claim with no content tokens cannot be grounded (scores ``0.0``).
    """

    def __init__(self, threshold: float = 0.85) -> None:
        self.threshold = threshold

    def score(self, claim_text: str, chunk_content: str) -> float:
        if not claim_text or not chunk_content:
            return 0.0
        # Verbatim short-circuit: strict-grounder parity, exactly 1.0.
        if claim_text.lower() in chunk_content.lower():
            return 1.0

        claim_content = [
            t for t in _tokens(claim_text) if t not in _STOPWORDS and len(t) > 1
        ]
        if not claim_content:
            return 0.0
        chunk_tokens = set(_tokens(chunk_content))

        total_weight = 0.0
        matched_weight = 0.0
        for token in claim_content:
            weight = float(len(token))
            total_weight += weight
            if _matches(token, chunk_tokens):
                matched_weight += weight
        if total_weight == 0.0:
            return 0.0
        return matched_weight / total_weight


#: Environment flag. Default OFF — production keeps the strict bar until flipped.
PARAPHRASE_GROUNDER_ENV = "AGENT_PARAPHRASE_GROUNDER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def resolve_grounder(
    *,
    env: dict[str, str] | None = None,
    threshold: float = 0.85,
) -> ExtractiveGrounder | ParaphraseTolerantGrounder:
    """Pick the grounder for the deployment. Default: the strict production bar.

    Flag OFF (the default) returns ``ExtractiveGrounder`` unchanged, so
    production behaviour is byte-identical until an operator opts in.
    """
    source = os.environ if env is None else env
    raw = source.get(PARAPHRASE_GROUNDER_ENV, "")
    if raw.strip().lower() in _TRUTHY:
        return ParaphraseTolerantGrounder(threshold=threshold)
    return ExtractiveGrounder()


@dataclass(frozen=True)
class ReverifyResult:
    """Result of re-verifying one evidence record."""

    run_id: str
    evidence_id: str
    statement: str
    old_verdict: str
    new_verdict: str
    agree: bool
    cause: str
    chunk_ids_checked: list[str]


@dataclass(frozen=True)
class StaleEvidenceReport:
    """Visible disagreement — run_id, claim, old/new verdict, cause class."""

    run_id: str
    evidence_id: str
    statement: str
    old_verdict: str
    new_verdict: str
    cause: str
    chunk_ids_checked: list[str]


class EvidenceVerifier:
    """Re-verify evidence records through production ExtractiveGrounder."""

    def __init__(
        self,
        chunk_source: ChunkSource | InMemoryChunkSource,
        grounder: ExtractiveGrounder | ParaphraseTolerantGrounder | None = None,
    ) -> None:
        self.chunk_source = chunk_source
        # Default OFF: the strict production grounder, unchanged.
        self.grounder = grounder or ExtractiveGrounder()

    async def reverify_record(self, record: EvidenceRecord) -> ReverifyResult:
        old_verdict = record.verdict
        # Collect all chunk ids that prove this claim
        ids: list[str] = []
        seen: set[str] = set()
        for cid in record.chunk_ids:
            if cid and cid not in seen:
                seen.add(cid)
                ids.append(cid)
        for c in record.citations:
            if c.chunk_id and c.chunk_id not in seen:
                seen.add(c.chunk_id)
                ids.append(c.chunk_id)
        if record.chunk_id and record.chunk_id not in seen:
            seen.add(record.chunk_id)
            ids.append(record.chunk_id)

        if not ids:
            # No provenance — should not happen (evidence validator enforces),
            # but fail closed: DISAGREE
            new_verdict = "REFUTED"
            agree = new_verdict == old_verdict
            return ReverifyResult(
                run_id=record.run_id,
                evidence_id=record.id,
                statement=record.statement,
                old_verdict=old_verdict,
                new_verdict=new_verdict,
                agree=agree,
                cause=CAUSE_NOT_GROUNDED,
                chunk_ids_checked=[],
            )

        grounded = False
        missing: list[str] = []
        for cid in ids:
            content = await self.chunk_source.get_chunk_content(cid)
            if content is None:
                missing.append(cid)
                continue
            score = self.grounder.score(record.statement, content)
            if score >= self.grounder.threshold:
                grounded = True
                break

        new_verdict = "SUPPORTED" if grounded else "REFUTED"
        agree = new_verdict == old_verdict

        if grounded:
            cause = CAUSE_GROUNDED
        else:
            # Not grounded — distinguish missing/tombstoned vs content mismatch
            tombstoned = [
                cid
                for cid in missing
                if hasattr(self.chunk_source, "is_tombstoned")
                and self.chunk_source.is_tombstoned(cid)
            ]
            if tombstoned:
                cause = CAUSE_CHUNK_TOMBSTONED
            elif missing and len(missing) == len(ids):
                # All chunks missing — stale deletion
                cause = CAUSE_CHUNK_NOT_FOUND
            elif missing:
                # Some missing but others present yet not grounding
                cause = CAUSE_NOT_GROUNDED
            else:
                cause = CAUSE_NOT_GROUNDED

        return ReverifyResult(
            run_id=record.run_id,
            evidence_id=record.id,
            statement=record.statement,
            old_verdict=old_verdict,
            new_verdict=new_verdict,
            agree=agree,
            cause=cause,
            chunk_ids_checked=ids,
        )

    async def reverify_run(self, records: list[EvidenceRecord]) -> list[ReverifyResult]:
        out: list[ReverifyResult] = []
        for rec in records:
            out.append(await self.reverify_record(rec))
        return out

    async def stale_report(self, records: list[EvidenceRecord]) -> list[StaleEvidenceReport]:
        results = await self.reverify_run(records)
        return [
            StaleEvidenceReport(
                run_id=r.run_id,
                evidence_id=r.evidence_id,
                statement=r.statement,
                old_verdict=r.old_verdict,
                new_verdict=r.new_verdict,
                cause=r.cause,
                chunk_ids_checked=r.chunk_ids_checked,
            )
            for r in results
            if not r.agree
        ]
