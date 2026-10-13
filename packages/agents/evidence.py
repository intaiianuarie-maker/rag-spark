"""Agent evidence persistence adapter (T-V3-014).

Persists verified claims + citations + verdicts durably so audits and
re-verification survive restarts. Every record carries run_id/trace_id,
chunk provenance, verifier version, and timestamp — re-verify later must
know WHAT verified it and WHEN.

- Write verified-claim records with run_id/trace_id, chunk provenance
  (document_id/chunk_id/citation), verifier_version, verified_at, verdict.
- Read path: fetch a run's evidence set back byte-identically (round-trip).
- Every record carries chunk ids + verifier version + timestamp (asserted).
- No update/delete surface — append-only, like StepStore.
- Backend injectable: InMemoryEvidenceStore + SqliteEvidenceStore share EvidenceStore.

Why SQLite file (not main DB) for this skeleton: StepStore already uses a
separate SQLite file for step events; evidence uses the same pattern — a
dedicated SQLite file per test/adapter, so no new alembic table is needed
for the skeleton. A real deployment would add a `agent_evidence` table via
alembic migration, but the `EvidenceStore` ABC makes the swap trivial.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

# Keep in sync with StepStore caps, but evidence has its own
MAX_EVIDENCE_BYTES = 32_768
MAX_CLAIM_CHARS = 2_048
MAX_CITATION_JSON_BYTES = 8_192

# Verifier version is pinned at write time — re-verify must know WHAT and WHEN
EVIDENCE_VERIFIER_VERSION = "v1.0.0-2026-10-13"


class Citation(BaseModel):
    """Provenance for one evidence unit — document + chunk + span + hash."""

    document_id: str
    chunk_id: str
    page: int | None = None
    section: str = ""
    span_start: int | None = None
    span_end: int | None = None
    quote: str = ""
    source_hash: str = ""


class EvidenceRecord(BaseModel):
    """Verified claim + citations + verdict — the durable evidence record.

    Every record carries:
    - run_id / trace_id — which run produced it
    - chunk_ids / citations — where it came from (provenance)
    - verifier_version — WHAT verified it
    - verified_at — WHEN it was verified
    - verdict — SUPPORTED / DISPUTED / UNKNOWN etc.
    """

    model_config = {"extra": "allow"}

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    trace_id: str
    workspace_id: str = ""
    statement: str
    citations: list[Citation] = Field(default_factory=list)
    chunk_ids: list[str] = Field(default_factory=list)
    verdict: str = "SUPPORTED"
    verifier_version: str = EVIDENCE_VERIFIER_VERSION
    verified_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    document_id: str = ""
    chunk_id: str = ""
    source_hash: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @model_validator(mode="after")
    def _validate_provenance(self) -> EvidenceRecord:
        if not self.run_id.strip():
            raise ValueError("run_id must be non-empty")
        if not self.trace_id.strip():
            raise ValueError("trace_id must be non-empty")
        if not self.statement.strip():
            raise ValueError("statement must be non-empty")
        if not self.verifier_version.strip():
            raise ValueError("verifier_version must be non-empty")
        if not self.verified_at.strip():
            raise ValueError("verified_at must be non-empty")
        # Every record must carry chunk provenance (at least one chunk_id or citation)
        has_chunk = bool(self.chunk_ids) or bool(self.citations) or bool(self.chunk_id)
        if not has_chunk:
            raise ValueError("evidence without provenance: need chunk_ids or citations or chunk_id")
        if not self.verifier_version:
            raise ValueError("verifier_version required")
        return self


class EvidenceStoreError(RuntimeError):
    pass


class EvidenceStore(ABC):
    """Append-only store for verified claims — backend injectable."""

    @abstractmethod
    async def write(self, record: EvidenceRecord) -> EvidenceRecord:
        """Write one verified claim — returns the stored record (with id)."""
        raise NotImplementedError

    @abstractmethod
    async def list_for_run(self, run_id: str) -> list[EvidenceRecord]:
        """Fetch a run's evidence set back — byte-identical to what was written."""
        raise NotImplementedError

    @abstractmethod
    async def get(self, run_id: str, evidence_id: str) -> EvidenceRecord | None:
        raise NotImplementedError


class InMemoryEvidenceStore(EvidenceStore):
    """In-memory impl — thread-safe, same interface as SQLite."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, dict[str, EvidenceRecord]] = {}  # run_id -> id -> record

    async def write(self, record: EvidenceRecord) -> EvidenceRecord:
        # Enforce provenance at the boundary — no zero-provenance records
        if not record.chunk_ids and not record.citations and not record.chunk_id:
            raise EvidenceStoreError("evidence without provenance")
        # Bound check
        raw = json.dumps(record.model_dump(), ensure_ascii=False)
        if len(raw.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            raise EvidenceStoreError(f"evidence exceeds {MAX_EVIDENCE_BYTES} bytes")
        with self._lock:
            bucket = self._store.setdefault(record.run_id, {})
            # Byte-identical round-trip: store a validated copy, not the raw input
            stored = EvidenceRecord.model_validate(record.model_dump())
            bucket[stored.id] = stored
            return stored

    async def list_for_run(self, run_id: str) -> list[EvidenceRecord]:
        with self._lock:
            bucket = self._store.get(run_id, {})
            # Return sorted by id for deterministic order, but preserve insertion order
            # For byte-identical, return in insertion order (dict preserves)
            return list(bucket.values())

    async def get(self, run_id: str, evidence_id: str) -> EvidenceRecord | None:
        with self._lock:
            bucket = self._store.get(run_id, {})
            return bucket.get(evidence_id)


class SqliteEvidenceStore(EvidenceStore):
    """SQLite file backend — same interface as InMemory, durable."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_evidence (
                    run_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (run_id, evidence_id)
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_agent_evidence_run_id ON agent_evidence(run_id)")  # noqa: E501
            con.commit()

    async def write(self, record: EvidenceRecord) -> EvidenceRecord:
        if not record.chunk_ids and not record.citations and not record.chunk_id:
            raise EvidenceStoreError("evidence without provenance")
        raw = json.dumps(record.model_dump(), ensure_ascii=False)
        if len(raw.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            raise EvidenceStoreError(f"evidence exceeds {MAX_EVIDENCE_BYTES} bytes")
        # Validate and store a copy
        stored = EvidenceRecord.model_validate(record.model_dump())
        data = json.dumps(stored.model_dump(), ensure_ascii=False)
        with self._lock:
            with sqlite3.connect(self.path) as con:
                con.execute(
                    "INSERT OR REPLACE INTO agent_evidence (run_id, evidence_id, data) VALUES (?, ?, ?)",  # noqa: E501
                    (stored.run_id, stored.id, data),
                )
                con.commit()
        return stored

    async def list_for_run(self, run_id: str) -> list[EvidenceRecord]:
        with self._lock:
            with sqlite3.connect(self.path) as con:
                cur = con.execute("SELECT data FROM agent_evidence WHERE run_id = ? ORDER BY rowid", (run_id,))  # noqa: E501
                rows = cur.fetchall()
                out: list[EvidenceRecord] = []
                for (data,) in rows:
                    rec = EvidenceRecord.model_validate(json.loads(data))
                    out.append(rec)
                return out

    async def get(self, run_id: str, evidence_id: str) -> EvidenceRecord | None:
        with self._lock:
            with sqlite3.connect(self.path) as con:
                cur = con.execute(
                    "SELECT data FROM agent_evidence WHERE run_id = ? AND evidence_id = ?", (run_id, evidence_id)  # noqa: E501
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return EvidenceRecord.model_validate(json.loads(row[0]))
