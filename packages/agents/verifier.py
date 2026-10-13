"""Claim/citation verifier — re-verify evidence via production grounder (T-V3-015).

Re-runs stored evidence through the SAME bar as production verification
(ExtractiveGrounder) — reuse the V1 verification path, do not build a second
grounder. Given a stored EvidenceRecord, re-fetch its chunks (read-only),
re-ground its statement, and verdict AGREE/DISAGREE with the stored verdict.
Disagreements surface as a stale-evidence report: run_id, claim, old verdict,
new verdict, cause class.

One-grounder rule: imports ExtractiveGrounder from answer.verification.
No second grounder lives here.

Chunk fetches are injectable via ChunkSource so tests never touch live DB
(T-071 scratch seam). The verifier itself is pure: it only reads.
"""

from __future__ import annotations

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
        grounder: ExtractiveGrounder | None = None,
    ) -> None:
        self.chunk_source = chunk_source
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
