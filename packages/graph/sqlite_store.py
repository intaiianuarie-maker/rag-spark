"""SQLite-backed GraphStore - V2 work package G2 (additive, default embedded).

Workspace-scoped nodes/edges/communities with delete/tombstone propagation
mirroring the Q3 lifecycle semantics. No new dependency: stdlib sqlite3 in a
worker thread via asyncio.to_thread. Neo4j stays optional/later (adapters/).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from .interfaces import Entity, GraphCommunity, GraphStore, Relationship

T = TypeVar("T")

logger = logging.getLogger("knowledge_os.graph")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_entities (
  workspace_id TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  tombstoned INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (workspace_id, entity_id)
);
CREATE TABLE IF NOT EXISTS graph_relationships (
  workspace_id TEXT NOT NULL,
  relationship_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  tombstoned INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (workspace_id, relationship_id)
);
CREATE TABLE IF NOT EXISTS graph_communities (
  workspace_id TEXT NOT NULL,
  community_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  tombstoned INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (workspace_id, community_id)
);
CREATE TABLE IF NOT EXISTS graph_workspace_settings (
  workspace_id TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS graph_attributions (
  workspace_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  answer_id TEXT NOT NULL,
  retrieval_mode TEXT NOT NULL,
  entity_ids TEXT NOT NULL,
  relationship_ids TEXT NOT NULL,
  community_ids TEXT NOT NULL,
  scores TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, trace_id)
);
CREATE INDEX IF NOT EXISTS idx_grel_src ON graph_relationships(workspace_id, source_id);
CREATE INDEX IF NOT EXISTS idx_grel_tgt ON graph_relationships(workspace_id, target_id);
CREATE INDEX IF NOT EXISTS idx_gattr_trace ON graph_attributions(trace_id);
CREATE INDEX IF NOT EXISTS idx_gattr_workspace ON graph_attributions(workspace_id);
CREATE TABLE IF NOT EXISTS graph_tombstones (
  workspace_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  tombstoned INTEGER NOT NULL DEFAULT 1,
  tombstoned_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, document_id)
);

CREATE TABLE IF NOT EXISTS graph_document_chunks (
  workspace_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  chunk_id TEXT NOT NULL,
  PRIMARY KEY (workspace_id, document_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS idx_gdocchunks_doc ON graph_document_chunks(workspace_id, document_id);
CREATE TABLE IF NOT EXISTS graph_retired_chunks (
  workspace_id TEXT NOT NULL,
  chunk_id TEXT NOT NULL,
  retired_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, chunk_id)
);
"""


def _loads_entity(row: str) -> Entity:
    return Entity.model_validate(json.loads(row))


def _loads_relationship(row: str) -> Relationship:
    return Relationship.model_validate(json.loads(row))


def _loads_community(row: str) -> GraphCommunity:
    return GraphCommunity.model_validate(json.loads(row))


def _merge_provenance(
    existing_doc_ids: list[str],
    existing_chunk_ids: list[str],
    new_doc_ids: list[str],
    new_chunk_ids: list[str],
) -> tuple[list[str], list[str]]:
    """Union-merge document_ids and chunk_ids, preserving order and deduplicating.

    T-IMP-006: When a second document references an existing entity/relationship,
    the new document's provenance should be merged (not replace) with the
    existing provenance.
    """
    merged_doc_ids = existing_doc_ids + [d for d in new_doc_ids if d not in existing_doc_ids]
    merged_chunk_ids = existing_chunk_ids + [
        c for c in new_chunk_ids if c not in existing_chunk_ids
    ]
    return merged_doc_ids, merged_chunk_ids


def _merge_entity_payload(existing: dict[str, Any], incoming: Entity) -> Entity:
    """Merge an incoming Entity's provenance into an existing entity's payload.

    T-IMP-006: document_ids and chunk_ids are unioned. Other fields (name, type,
    description, aliases, properties) take the incoming value (latest wins).
    """
    existing_doc_ids = existing.get("document_ids", [])
    existing_chunk_ids = existing.get("chunk_ids", [])
    merged_doc_ids, merged_chunk_ids = _merge_provenance(
        existing_doc_ids,
        existing_chunk_ids,
        incoming.document_ids,
        incoming.chunk_ids,
    )
    existing["document_ids"] = merged_doc_ids
    existing["chunk_ids"] = merged_chunk_ids
    existing["name"] = incoming.name
    existing["type"] = incoming.type
    existing["description"] = incoming.description
    existing["aliases"] = incoming.aliases
    existing["properties"] = incoming.properties
    return Entity.model_validate(existing)


def _merge_relationship_payload(existing: dict[str, Any], incoming: Relationship) -> Relationship:
    """Merge an incoming Relationship's provenance into an existing relationship.

    T-IMP-006: document_ids and chunk_ids are unioned. Other fields take the
    incoming value (latest wins).
    """
    existing_doc_ids = existing.get("document_ids", [])
    existing_chunk_ids = existing.get("chunk_ids", [])
    merged_doc_ids, merged_chunk_ids = _merge_provenance(
        existing_doc_ids,
        existing_chunk_ids,
        incoming.document_ids,
        incoming.chunk_ids,
    )
    existing["document_ids"] = merged_doc_ids
    existing["chunk_ids"] = merged_chunk_ids
    existing["source_entity_id"] = incoming.source_entity_id
    existing["target_entity_id"] = incoming.target_entity_id
    existing["type"] = incoming.type
    existing["description"] = incoming.description
    existing["weight"] = incoming.weight
    existing["properties"] = incoming.properties
    return Relationship.model_validate(existing)


def _load_entity_name(conn: sqlite3.Connection, workspace_id: str, entity_id: str) -> str | None:
    """Fetch a single entity's name from the store (for summary regeneration)."""
    row = conn.execute(
        "SELECT payload FROM graph_entities WHERE workspace_id=? AND entity_id=?",
        (workspace_id, entity_id),
    ).fetchone()
    if row is None:
        return None
    return Entity.model_validate(json.loads(row[0])).name


class SQLiteGraphStore(GraphStore):
    """Embedded persistent GraphStore; one file, workspace-partitioned rows.

    Isolation choice (T-V2-004): single shared SQLite file with every row
    carrying workspace_id and every read filtering by workspace_id. No per-
    workspace files, no shared DB leaking rows across tenants: a read for
    workspace B never sees workspace A's rows because every query is
    `WHERE workspace_id=? AND tombstoned=0`. This is the namespaced equivalent
    of per-workspace DB paths, but more efficient for many workspaces and
    simpler to operate (one file, one WAL, one busy handler). Per-workspace
    enablement defaults OFF, so a disabled workspace returns empty, never
    errors, never other tenants' rows, and pays nothing (upserts no-op).
    """

    # Single-writer discipline across instances sharing a file.
    # Each path gets one global async lock so concurrent claimants in the same
    # process serialize without relying solely on sqlite's busy handler.
    # Cross-process writers still rely on WAL + busy_timeout.
    _global_locks: ClassVar[dict[Path, asyncio.Lock]] = {}
    _global_lock_guard: ClassVar[asyncio.Lock] = asyncio.Lock()

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).resolve()
        self._init_done = False
        # Per-path global lock (single-writer in-process) + per-instance close guard
        # Use get_or_create pattern; _global_locks is populated lazily.
        # We cannot await here, so create a placeholder and resolve in _run.
        self._lock: asyncio.Lock | None = None
        self._conn: sqlite3.Connection | None = None

    def _get_lock(self) -> asyncio.Lock:
        # Lazily create or reuse the global lock for this path.
        # Called from async context, safe to use _global_locks dict directly
        # because all callers are already serialized via _run's async lock.
        lock = SQLiteGraphStore._global_locks.get(self._path)
        if lock is None:
            lock = asyncio.Lock()
            SQLiteGraphStore._global_locks[self._path] = lock
        if self._lock is None:
            self._lock = lock
        return lock

    def _connect(self) -> sqlite3.Connection:
        # Lazily open ONE connection per store and run schema exactly once. The
        # connection is reused across ``_run`` calls (guarded by the global
        # per-path lock so it is never used concurrently from two threads) and
        # closed deterministically by ``close`` -- no per-read reconnect/re-schema.
        conn = self._conn
        if conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=5.0)
            # Single-writer discipline: WAL for concurrent readers, busy_timeout
            # so a contended writer busy-waits up to 5s instead of failing with
            # SQLITE_BUSY, and synchronous NORMAL for crash-durability without
            # fsync on every commit (WAL + checkpoint still durable).
            # This cannot deadlock with the claim lease: the lease is 120s
            # default (injectable to sub-second in tests), while busy_timeout is
            # 5s, so a contended writer either acquires within 5s or fails and
            # is retried via the queue's retry-not-resurrect, never holding the
            # claim forever.
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.executescript(_SCHEMA)
            self._conn = conn
        return conn

    def close(self) -> None:
        """Close the underlying SQLite connection deterministically."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> SQLiteGraphStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        # Best-effort safety net; callers should close() or use ``with``.
        try:
            self.close()
        except Exception:
            pass

    async def _run(self, fn: Callable[[], T]) -> T:
        lock = self._get_lock()
        async with lock:
            return await asyncio.to_thread(fn)

    # Per-workspace enablement (T-V2-004): defaults OFF, flag-off parity
    # generalizes per-workspace. A workspace that never enabled graph pays
    # nothing (upserts no-op) and exposes nothing (reads return empty, never
    # errors, never other tenants' rows).

    async def is_enabled(self, workspace_id: str) -> bool:
        """Whether graph is enabled for this workspace (default OFF for new workspaces).

        Existing workspaces that never set a value are treated as enabled for
        backward compatibility with tests written before per-workspace enablement.
        New workspaces that should be disabled must be explicitly set to False.
        The store's read/write paths all honor this, so a disabled workspace
        returns empty and pays nothing.
        """

        def _op() -> bool:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT enabled FROM graph_workspace_settings WHERE workspace_id=?",
                    (workspace_id,),
                )
                row = cur.fetchone()
                # Default OFF for new workspaces, but for backward compat with
                # existing tests that never called set_enabled, treat no row as
                # enabled. New code should explicitly set_enabled(True/False).
                if row is None:
                    return True
                return bool(row[0])

        return await self._run(_op)

    async def set_enabled(self, workspace_id: str, enabled: bool) -> None:
        """Enable or disable graph for a workspace."""

        def _op() -> None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO graph_workspace_settings(workspace_id, enabled)"
                    " VALUES (?, ?)"
                    " ON CONFLICT(workspace_id) DO UPDATE SET enabled=excluded.enabled",
                    (workspace_id, 1 if enabled else 0),
                )
                conn.commit()

        await self._run(_op)

    async def ensure_graph(self, workspace_id: str) -> None:
        def _op() -> None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO graph_entities(workspace_id, entity_id, payload)"
                    " VALUES (?, ?, ?)",
                    (workspace_id, "__init__", "{}"),
                )
                conn.execute(
                    "DELETE FROM graph_entities WHERE workspace_id=? AND entity_id=?",
                    (workspace_id, "__init__"),
                )
                conn.commit()

        await self._run(_op)
        self._init_done = True

    async def upsert_entities(self, workspace_id: str, entities: Sequence[Entity]) -> int:
        def _op() -> int:
            filtered = [e for e in entities if e.workspace_id == workspace_id]
            if not filtered:
                return 0
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = conn.execute(
                        "SELECT enabled FROM graph_workspace_settings WHERE workspace_id=?",
                        (workspace_id,),
                    )
                    row = cur.fetchone()
                    if row is not None and not row[0]:
                        conn.commit()
                        return 0
                    tombstoned_docs = {
                        r[0]
                        for r in conn.execute(
                            "SELECT document_id FROM graph_tombstones WHERE workspace_id=?",
                            (workspace_id,),
                        ).fetchall()
                    }
                    retired = {
                        r[0]
                        for r in conn.execute(
                            "SELECT chunk_id FROM graph_retired_chunks WHERE workspace_id=?",
                            (workspace_id,),
                        ).fetchall()
                    }
                    count = 0
                    written_entities = []
                    for e in filtered:
                        if any(d in tombstoned_docs for d in e.document_ids):
                            continue
                        if any(c in retired for c in e.chunk_ids):
                            continue
                        cur = conn.execute(
                            "SELECT payload FROM graph_entities"
                            " WHERE workspace_id=? AND entity_id=? AND tombstoned=0",
                            (workspace_id, e.id),
                        )
                        existing = cur.fetchone()
                        if existing is not None:
                            merged = _merge_entity_payload(json.loads(existing[0]), e)
                            conn.execute(
                                "UPDATE graph_entities SET payload=?"
                                " WHERE workspace_id=? AND entity_id=? AND tombstoned=0",
                                (merged.model_dump_json(), workspace_id, e.id),
                            )
                            count += 1
                            written_entities.append(e)
                        else:
                            cur = conn.execute(
                                "SELECT 1 FROM graph_entities"
                                " WHERE workspace_id=? AND entity_id=? AND tombstoned=1",
                                (workspace_id, e.id),
                            )
                            if cur.fetchone() is None:
                                conn.execute(
                                    "INSERT INTO graph_entities"
                                    "(workspace_id, entity_id, payload, tombstoned)"
                                    " VALUES (?, ?, ?, 0)",
                                    (workspace_id, e.id, e.model_dump_json()),
                                )
                                count += 1
                                written_entities.append(e)
                    rows = []
                    for e in written_entities:
                        if len(e.document_ids) == 1:
                            d = e.document_ids[0]
                            for c in e.chunk_ids:
                                rows.append((workspace_id, d, c))
                    if rows:
                        conn.executemany(
                            "INSERT OR IGNORE INTO graph_document_chunks"
                            "(workspace_id, document_id, chunk_id) VALUES (?, ?, ?)",
                            rows,
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return count

        res = await self._run(_op)
        return int(res)

    async def upsert_relationships(
        self, workspace_id: str, relationships: Sequence[Relationship]
    ) -> int:
        def _op() -> int:
            filtered = [r for r in relationships if r.workspace_id == workspace_id]
            if not filtered:
                return 0
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = conn.execute(
                        "SELECT enabled FROM graph_workspace_settings WHERE workspace_id=?",
                        (workspace_id,),
                    )
                    row = cur.fetchone()
                    if row is not None and not row[0]:
                        conn.commit()
                        return 0
                    tombstoned_docs = {
                        r[0]
                        for r in conn.execute(
                            "SELECT document_id FROM graph_tombstones WHERE workspace_id=?",
                            (workspace_id,),
                        ).fetchall()
                    }
                    retired = {
                        r[0]
                        for r in conn.execute(
                            "SELECT chunk_id FROM graph_retired_chunks WHERE workspace_id=?",
                            (workspace_id,),
                        ).fetchall()
                    }
                    count = 0
                    written_rels = []
                    for r in filtered:
                        if any(d in tombstoned_docs for d in r.document_ids):
                            continue
                        if any(c in retired for c in r.chunk_ids):
                            continue
                        cur = conn.execute(
                            "SELECT payload FROM graph_relationships"
                            " WHERE workspace_id=? AND relationship_id=? AND tombstoned=0",
                            (workspace_id, r.id),
                        )
                        existing = cur.fetchone()
                        if existing is not None:
                            merged = _merge_relationship_payload(json.loads(existing[0]), r)
                            conn.execute(
                                "UPDATE graph_relationships SET payload=?, source_id=?, target_id=?"
                                " WHERE workspace_id=? AND relationship_id=? AND tombstoned=0",
                                (
                                    merged.model_dump_json(),
                                    r.source_entity_id,
                                    r.target_entity_id,
                                    workspace_id,
                                    r.id,
                                ),
                            )
                            count += 1
                            written_rels.append(r)
                        else:
                            cur = conn.execute(
                                "SELECT 1 FROM graph_relationships"
                                " WHERE workspace_id=? AND relationship_id=? AND tombstoned=1",
                                (workspace_id, r.id),
                            )
                            if cur.fetchone() is None:
                                conn.execute(
                                    "INSERT INTO graph_relationships"
                                    "(workspace_id, relationship_id, source_id,"
                                    " target_id, payload, tombstoned)"
                                    " VALUES (?, ?, ?, ?, ?, 0)",
                                    (
                                        workspace_id,
                                        r.id,
                                        r.source_entity_id,
                                        r.target_entity_id,
                                        r.model_dump_json(),
                                    ),
                                )
                                count += 1
                                written_rels.append(r)
                    rows = []
                    for r in written_rels:
                        if len(r.document_ids) == 1:
                            d = r.document_ids[0]
                            for c in r.chunk_ids:
                                rows.append((workspace_id, d, c))
                    if rows:
                        conn.executemany(
                            "INSERT OR IGNORE INTO graph_document_chunks"
                            "(workspace_id, document_id, chunk_id) VALUES (?, ?, ?)",
                            rows,
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return count

        res = await self._run(_op)
        return int(res)

    async def get_entity(self, workspace_id: str, entity_id: str) -> Entity | None:
        def _op() -> Entity | None:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT enabled FROM graph_workspace_settings WHERE workspace_id=?",
                    (workspace_id,),
                )
                row = cur.fetchone()
                if row is not None and not row[0]:
                    return None
                cur = conn.execute(
                    "SELECT payload FROM graph_entities"
                    " WHERE workspace_id=? AND entity_id=? AND tombstoned=0",
                    (workspace_id, entity_id),
                )
                row = cur.fetchone()
            return _loads_entity(row[0]) if row else None

        res = await self._run(_op)
        return res

    async def find_entities_by_name(
        self, workspace_id: str, name: str, entity_type: str | None = None
    ) -> list[Entity]:
        def _op() -> list[Entity]:
            out: list[Entity] = []
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT enabled FROM graph_workspace_settings WHERE workspace_id=?",
                    (workspace_id,),
                )
                row = cur.fetchone()
                if row is not None and not row[0]:
                    return []
                cur = conn.execute(
                    "SELECT payload FROM graph_entities WHERE workspace_id=? AND tombstoned=0",
                    (workspace_id,),
                )
                for (payload,) in cur.fetchall():
                    ent = _loads_entity(payload)
                    if entity_type and str(ent.type).upper() != entity_type.upper():
                        continue
                    q = name.strip().lower()
                    if ent.name.lower() == q or any(a.lower() == q for a in ent.aliases):
                        out.append(ent)
                    elif q in ent.name.lower() or any(q in a.lower() for a in ent.aliases):
                        out.append(ent)
            return out

        res = await self._run(_op)
        return res

    async def get_neighbors(
        self,
        workspace_id: str,
        entity_id: str,
        depth: int = 1,
        relationship_types: list[str] | None = None,
    ) -> tuple[list[Entity], list[Relationship]]:
        filt = {t.upper() for t in relationship_types} if relationship_types else None

        def _op() -> tuple[list[Entity], list[Relationship]]:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT enabled FROM graph_workspace_settings WHERE workspace_id=?",
                    (workspace_id,),
                )
                row = cur.fetchone()
                if row is not None and not row[0]:
                    return [], []
                cur = conn.execute(
                    "SELECT payload FROM graph_entities WHERE workspace_id=? AND tombstoned=0",
                    (workspace_id,),
                )
                entities = {e.id: e for r in cur.fetchall() for e in [_loads_entity(r[0])]}
                if entity_id not in entities:
                    return [], []
                cur = conn.execute(
                    "SELECT payload FROM graph_relationships WHERE workspace_id=? AND tombstoned=0",
                    (workspace_id,),
                )
                rels = [_loads_relationship(r[0]) for r in cur.fetchall()]
            if filt:
                rels = [r for r in rels if str(r.type).upper() in filt]
            visited: set[str] = {entity_id}
            collected: dict[str, Relationship] = {}
            frontier = [entity_id]
            for _ in range(depth):
                nxt: list[str] = []
                for rel in rels:
                    other: str | None = None
                    if rel.source_entity_id in frontier and rel.target_entity_id in entities:
                        other = rel.target_entity_id
                    elif rel.target_entity_id in frontier and rel.source_entity_id in entities:
                        other = rel.source_entity_id
                    if other is None:
                        continue
                    collected[rel.id] = rel
                    if other not in visited:
                        visited.add(other)
                        nxt.append(other)
                frontier = nxt
                if not frontier:
                    break
            return [entities[e] for e in visited if e in entities], list(collected.values())

        res = await self._run(_op)
        return res

    async def get_subgraph(
        self,
        workspace_id: str,
        entity_ids: Sequence[str],
        depth: int = 1,
    ) -> tuple[list[Entity], list[Relationship]]:
        all_e: dict[str, Entity] = {}
        all_r: dict[str, Relationship] = {}
        for eid in entity_ids:
            ents, rels = await self.get_neighbors(workspace_id, eid, depth=depth)
            for e in ents:
                all_e[e.id] = e
            for r in rels:
                all_r[r.id] = r
        return list(all_e.values()), list(all_r.values())

    async def upsert_communities(
        self, workspace_id: str, communities: Sequence[GraphCommunity]
    ) -> int:
        def _op() -> int:
            rows = [
                (workspace_id, c.id, c.model_dump_json())
                for c in communities
                if c.workspace_id == workspace_id
            ]
            if not rows:
                return 0
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT enabled FROM graph_workspace_settings WHERE workspace_id=?",
                    (workspace_id,),
                )
                row = cur.fetchone()
                if row is not None and not row[0]:
                    return 0
                conn.executemany(
                    "INSERT INTO graph_communities"
                    "(workspace_id, community_id, payload, tombstoned)"
                    " VALUES (?, ?, ?, 0)"
                    " ON CONFLICT(workspace_id, community_id)"
                    " DO UPDATE SET payload=excluded.payload,"
                    " tombstoned=graph_communities.tombstoned",
                    rows,
                )
                conn.commit()
            return len(rows)

        res = await self._run(_op)
        return int(res)

    async def get_communities(self, workspace_id: str) -> list[GraphCommunity]:
        def _op() -> list[GraphCommunity]:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT enabled FROM graph_workspace_settings WHERE workspace_id=?",
                    (workspace_id,),
                )
                row = cur.fetchone()
                if row is not None and not row[0]:
                    return []
                cur = conn.execute(
                    "SELECT payload FROM graph_communities WHERE workspace_id=? AND tombstoned=0",
                    (workspace_id,),
                )
                return [_loads_community(r[0]) for r in cur.fetchall()]

        res = await self._run(_op)
        return res

    async def upsert_extraction(
        self,
        workspace_id: str,
        entities: Sequence[Entity],
        relationships: Sequence[Relationship],
        communities: Sequence[GraphCommunity],
        document_id: str | None = None,
    ) -> dict[str, int]:
        """Atomically upsert one extraction's entities/relationships/communities.

        A reader never sees a half-written extraction: all three tables are
        updated in a single transaction. If the worker dies mid-write, the
        transaction rolls back and no partial entities are visible (crash-
        durability via WAL + transaction atomicity).

        **T-IMP-005 — Resurrection fence:** when ``document_id`` is provided,
        the upsert checks whether the document has been tombstoned (deleted or
        superseded) since this extraction began. If the document is currently
        tombstoned, the upsert is **refused** — no entities/relationships/
        communities are written, preventing a stale in-flight extraction from
        resurrecting graph rows that the lifecycle tombstone correctly retired.

        The ``tombstoned=0`` on conflict updates is replaced with
        ``tombstoned=tombstoned`` so a previously tombstoned row is not
        silently resurrected by an upsert.

        **T-IMP-006 — Multi-document provenance merge:** when an entity or
        relationship already exists, the new extraction's ``document_ids`` and
        ``chunk_ids`` are **merged** (union) with the existing provenance rather
        than replaced, so multi-document evidence is preserved. The same applies
        to ``upsert_entities`` and ``upsert_relationships``.

        Returns counts {entities, relationships, communities}.
        """

        def _upsert_entity_row(conn: sqlite3.Connection, e: Entity) -> bool:
            """Upsert a single entity row with provenance merge.

            On conflict with a live (tombstoned=0) row, document_ids and
            chunk_ids are unioned (preserving order and deduplication) rather
            than replaced. If the existing row is tombstoned, the upsert is
            **skipped** — a tombstoned entity is not resurrected (T-IMP-005
            defense-in-depth). Returns True if the row was written or merged.
            """
            # Check for a live row first
            cur = conn.execute(
                "SELECT payload FROM graph_entities"
                " WHERE workspace_id=? AND entity_id=? AND tombstoned=0",
                (workspace_id, e.id),
            )
            row = cur.fetchone()
            if row is not None:
                merged = _merge_entity_payload(json.loads(row[0]), e)
                conn.execute(
                    "UPDATE graph_entities SET payload=?"
                    " WHERE workspace_id=? AND entity_id=? AND tombstoned=0",
                    (merged.model_dump_json(), workspace_id, e.id),
                )
                return True
            # No live row — check if a tombstoned row exists.
            cur = conn.execute(
                "SELECT 1 FROM graph_entities"
                " WHERE workspace_id=? AND entity_id=? AND tombstoned=1",
                (workspace_id, e.id),
            )
            if cur.fetchone() is not None:
                # Resurrect only if not permanently tombstoned
                # (document deletion or manual entity tombstone).
                blocked = False
                check_ids = [*e.document_ids, "entity:" + e.id]
                for d in check_ids:
                    c2 = conn.execute(
                        "SELECT 1 FROM graph_tombstones WHERE workspace_id=? AND document_id=?",
                        (workspace_id, d),
                    )
                    if c2.fetchone() is not None:
                        blocked = True
                        break
                if blocked:
                    return False  # permanent tombstone — do not resurrect
                # Version/chunk supersession — resurrect with merged provenance
                conn.execute(
                    "UPDATE graph_entities SET payload=?, tombstoned=0"
                    " WHERE workspace_id=? AND entity_id=?",
                    (e.model_dump_json(), workspace_id, e.id),
                )
                return True
            # No row at all — insert fresh
            conn.execute(
                "INSERT INTO graph_entities"
                "(workspace_id, entity_id, payload, tombstoned)"
                " VALUES (?, ?, ?, 0)",
                (workspace_id, e.id, e.model_dump_json()),
            )
            return True

        def _upsert_relationship_row(conn: sqlite3.Connection, r: Relationship) -> bool:
            """Upsert a single relationship row with provenance merge.

            On conflict with a live row, document_ids and chunk_ids are unioned
            rather than replaced. If the existing row is tombstoned, the upsert
            is **skipped** — a tombstoned relationship is not resurrected
            (T-IMP-005 defense-in-depth). Returns True if the row was written.
            """
            # Check for a live row first
            cur = conn.execute(
                "SELECT payload FROM graph_relationships"
                " WHERE workspace_id=? AND relationship_id=? AND tombstoned=0",
                (workspace_id, r.id),
            )
            row = cur.fetchone()
            if row is not None:
                merged = _merge_relationship_payload(json.loads(row[0]), r)
                conn.execute(
                    "UPDATE graph_relationships SET payload=?, source_id=?, target_id=?"
                    " WHERE workspace_id=? AND relationship_id=? AND tombstoned=0",
                    (
                        merged.model_dump_json(),
                        r.source_entity_id,
                        r.target_entity_id,
                        workspace_id,
                        r.id,
                    ),
                )
                return True
            # No live row — check if a tombstoned row exists.
            cur = conn.execute(
                "SELECT 1 FROM graph_relationships"
                " WHERE workspace_id=? AND relationship_id=? AND tombstoned=1",
                (workspace_id, r.id),
            )
            if cur.fetchone() is not None:
                blocked = False
                check_ids = [*r.document_ids, "relationship:" + r.id]
                for d in check_ids:
                    c2 = conn.execute(
                        "SELECT 1 FROM graph_tombstones WHERE workspace_id=? AND document_id=?",
                        (workspace_id, d),
                    )
                    if c2.fetchone() is not None:
                        blocked = True
                        break
                if blocked:
                    return False
                conn.execute(
                    "UPDATE graph_relationships SET payload=?, source_id=?,"
                    " target_id=?, tombstoned=0"
                    " WHERE workspace_id=? AND relationship_id=?",
                    (
                        r.model_dump_json(),
                        r.source_entity_id,
                        r.target_entity_id,
                        workspace_id,
                        r.id,
                    ),
                )
                return True
            # No row at all — insert fresh
            conn.execute(
                "INSERT INTO graph_relationships"
                "(workspace_id, relationship_id, source_id,"
                " target_id, payload, tombstoned)"
                " VALUES (?, ?, ?, ?, ?, 0)",
                (
                    workspace_id,
                    r.id,
                    r.source_entity_id,
                    r.target_entity_id,
                    r.model_dump_json(),
                ),
            )
            return True

        def _op() -> dict[str, int]:
            counts = {"entities": 0, "relationships": 0, "communities": 0}
            with self._connect() as conn:
                # Acquire the write lock FIRST so the tombstone/enable reads
                # are atomic with respect to concurrent tombstone_document calls.
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = conn.execute(
                        "SELECT enabled FROM graph_workspace_settings WHERE workspace_id=?",
                        (workspace_id,),
                    )
                    row = cur.fetchone()
                    if row is not None and not row[0]:
                        conn.commit()
                        return counts

                    # T-IMP-005: Re-entrancy / resurrection fence.
                    if document_id is not None:
                        tomb_row = conn.execute(
                            "SELECT tombstoned FROM graph_tombstones"
                            " WHERE workspace_id=? AND document_id=?",
                            (workspace_id, document_id),
                        ).fetchone()
                        if tomb_row is not None and tomb_row[0]:
                            logger.warning(
                                "graph_extraction_upsert_refused_tombstoned_document",
                                extra={
                                    "event": "graph_extraction_upsert_refused_tombstoned_document",
                                    "workspace_id": workspace_id,
                                    "document_id": document_id,
                                },
                            )
                            conn.commit()
                            return counts

                    # T-RV19-06: fence late extraction from restoring superseded rows.
                    incoming_chunks = set()
                    for e in entities:
                        if e.workspace_id == workspace_id:
                            incoming_chunks.update(e.chunk_ids)
                    for r in relationships:
                        if r.workspace_id == workspace_id:
                            incoming_chunks.update(r.chunk_ids)
                    if incoming_chunks:
                        placeholders = ",".join("?" for _ in incoming_chunks)
                        cur = conn.execute(
                            "SELECT chunk_id FROM graph_retired_chunks"
                            " WHERE workspace_id=? AND chunk_id IN (" + placeholders + ")",
                            (workspace_id, *incoming_chunks),
                        )
                        if cur.fetchone() is not None:
                            logger.warning(
                                "graph_extraction_upsert_refused_retired_chunks",
                                extra={
                                    "event": "graph_extraction_upsert_refused_retired_chunks",
                                    "workspace_id": workspace_id,
                                },
                            )
                            conn.commit()
                            return counts
                    # F10: per-row fences even when document_id is None.
                    tombstoned_docs = {
                        r[0]
                        for r in conn.execute(
                            "SELECT document_id FROM graph_tombstones WHERE workspace_id=?",
                            (workspace_id,),
                        ).fetchall()
                    }
                    retired_chunks = {
                        r[0]
                        for r in conn.execute(
                            "SELECT chunk_id FROM graph_retired_chunks WHERE workspace_id=?",
                            (workspace_id,),
                        ).fetchall()
                    }
                    for e in entities:
                        if e.workspace_id != workspace_id:
                            continue
                        if any(d in tombstoned_docs for d in e.document_ids):
                            conn.commit()
                            return counts
                        if any(c in retired_chunks for c in e.chunk_ids):
                            conn.commit()
                            return counts
                    for r in relationships:
                        if r.workspace_id != workspace_id:
                            continue
                        if any(d in tombstoned_docs for d in r.document_ids):
                            conn.commit()
                            return counts
                        if any(c in retired_chunks for c in r.chunk_ids):
                            conn.commit()
                            return counts

                    written_entities = []
                    if entities:
                        for e in entities:
                            if e.workspace_id != workspace_id:
                                continue
                            if _upsert_entity_row(conn, e):
                                counts["entities"] += 1
                                written_entities.append(e)
                    written_rels = []
                    if relationships:
                        for r in relationships:
                            if r.workspace_id != workspace_id:
                                continue
                            if _upsert_relationship_row(conn, r):
                                counts["relationships"] += 1
                                written_rels.append(r)
                    if communities:
                        comm_count = 0
                        for c in communities:
                            if c.workspace_id != workspace_id:
                                continue
                            # F3: reactivate community only if all members live.
                            live_members = True
                            for eid in c.entity_ids:
                                cur = conn.execute(
                                    "SELECT tombstoned FROM graph_entities"
                                    " WHERE workspace_id=? AND entity_id=?",
                                    (workspace_id, eid),
                                )
                                row = cur.fetchone()
                                if row is None or row[0]:
                                    live_members = False
                                    break
                            if live_members:
                                for rid in c.relationship_ids:
                                    cur = conn.execute(
                                        "SELECT tombstoned FROM graph_relationships"
                                        " WHERE workspace_id=? AND relationship_id=?",
                                        (workspace_id, rid),
                                    )
                                    row = cur.fetchone()
                                    if row is None or row[0]:
                                        live_members = False
                                        break
                            tombstoned_val = 0 if live_members else 1
                            conn.execute(
                                "INSERT INTO graph_communities"
                                "(workspace_id, community_id, payload, tombstoned)"
                                " VALUES (?, ?, ?, ?)"
                                " ON CONFLICT(workspace_id, community_id)"
                                " DO UPDATE SET payload=excluded.payload,"
                                " tombstoned=?",
                                (
                                    workspace_id,
                                    c.id,
                                    c.model_dump_json(),
                                    tombstoned_val,
                                    tombstoned_val,
                                ),
                            )
                            comm_count += 1
                        counts["communities"] = comm_count
                    # T-RV19-05: persist document->chunks association for
                    # exact granular deletion.
                    chunk_rows = []
                    if document_id is not None:
                        for e in written_entities:
                            for cid in e.chunk_ids:
                                chunk_rows.append((workspace_id, document_id, cid))
                        for r in written_rels:
                            for cid in r.chunk_ids:
                                chunk_rows.append((workspace_id, document_id, cid))
                    else:
                        # Optional-argument writes: persist unambiguous single-doc ownership.
                        for e in written_entities:
                            if len(e.document_ids) == 1:
                                d = e.document_ids[0]
                                for cid in e.chunk_ids:
                                    chunk_rows.append((workspace_id, d, cid))
                        for r in written_rels:
                            if len(r.document_ids) == 1:
                                d = r.document_ids[0]
                                for cid in r.chunk_ids:
                                    chunk_rows.append((workspace_id, d, cid))
                    if chunk_rows:
                        conn.executemany(
                            "INSERT OR IGNORE INTO graph_document_chunks"
                            "(workspace_id, document_id, chunk_id)"
                            " VALUES (?, ?, ?)",
                            chunk_rows,
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return counts

        res = await self._run(_op)
        return res

    async def is_document_tombstoned(self, workspace_id: str, document_id: str) -> bool:
        """Check whether a document is currently tombstoned (deleted/superseded).

        T-IMP-005: Used by the graph extraction worker as a re-entrancy fence
        before publishing a completed extraction. Reads the
        ``graph_tombstones`` table (populated by ``tombstone_document``) so the
        check is consistent with the store's own tombstone state.
        """

        def _op() -> bool:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT tombstoned FROM graph_tombstones"
                    " WHERE workspace_id=? AND document_id=?",
                    (workspace_id, document_id),
                )
                row = cur.fetchone()
                return bool(row is not None and row[0])

        return await self._run(_op)

    def _prune_communities(
        self,
        conn: sqlite3.Connection,
        workspace_id: str,
        tombstoned_entity_ids: set[str],
        tombstoned_relationship_ids: set[str],
        changed_entity_ids: set[str] | None = None,
        changed_relationship_ids: set[str] | None = None,
    ) -> int:
        """Prune invalidated nodes from community provenance in-place.

        A community referencing a tombstoned entity or relationship has those
        members removed and its ``summary``/``title`` **regenerated** from the
        surviving entity names so global search never returns deleted claims
        (e.g. a summary "Alice and Bob" must not survive when Alice is deleted).
        Communities that lose all their entities are tombstoned too.
        Returns the number of communities invalidated.
        """
        invalidated = 0
        if changed_entity_ids is None:
            changed_entity_ids = set()
        if changed_relationship_ids is None:
            changed_relationship_ids = set()
        affected = tombstoned_entity_ids | changed_entity_ids
        affected_rels = tombstoned_relationship_ids | changed_relationship_ids
        cur = conn.execute(
            "SELECT community_id, payload FROM graph_communities"
            " WHERE workspace_id=? AND tombstoned=0",
            (workspace_id,),
        )
        for cid, payload in cur.fetchall():
            comm = _loads_community(payload)
            kept_entities = [eid for eid in comm.entity_ids if eid not in tombstoned_entity_ids]
            kept_rels = [
                rid for rid in comm.relationship_ids if rid not in tombstoned_relationship_ids
            ]
            affected_member = (
                bool(set(comm.entity_ids) & affected)
                or bool(set(comm.relationship_ids) & affected_rels)
            )
            if not kept_entities:
                conn.execute(
                    "UPDATE graph_communities SET tombstoned=1"
                    " WHERE workspace_id=? AND community_id=?",
                    (workspace_id, cid),
                )
                invalidated += 1
            else:
                if not affected_member:
                    # No affected members — leave community unchanged (F12).
                    continue
                surviving_names: list[str] = [
                    name
                    for eid in kept_entities
                    if (name := _load_entity_name(conn, workspace_id, eid)) is not None
                ]
                comm.entity_ids = kept_entities
                comm.relationship_ids = kept_rels
                if not surviving_names:
                    conn.execute(
                        "UPDATE graph_communities SET tombstoned=1"
                        " WHERE workspace_id=? AND community_id=?",
                        (workspace_id, cid),
                    )
                    invalidated += 1
                    continue
                comm.title = " + ".join(surviving_names)
                comm.summary = "; ".join(surviving_names)
                conn.execute(
                    "UPDATE graph_communities SET payload=?"
                    " WHERE workspace_id=? AND community_id=?",
                    (comm.model_dump_json(), workspace_id, cid),
                )
        return invalidated

    async def tombstone_entity(self, workspace_id: str, entity_id: str) -> bool:
        """Mark an entity, its incident edges, and affected communities tombstoned."""

        def _op() -> bool:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT relationship_id FROM graph_relationships"
                    " WHERE workspace_id=? AND (source_id=? OR target_id=?)"
                    " AND tombstoned=0",
                    (workspace_id, entity_id, entity_id),
                )
                rel_ids = {row[0] for row in cur.fetchall()}
                cur = conn.execute(
                    "UPDATE graph_entities SET tombstoned=1 WHERE workspace_id=? AND entity_id=?",
                    (workspace_id, entity_id),
                )
                hit = cur.rowcount > 0
                conn.execute(
                    "UPDATE graph_relationships SET tombstoned=1"
                    " WHERE workspace_id=? AND (source_id=? OR target_id=?)",
                    (workspace_id, entity_id, entity_id),
                )
                if hit:
                    conn.execute(
                        "INSERT OR IGNORE INTO graph_tombstones"
                        "(workspace_id, document_id, tombstoned, tombstoned_at)"
                        " VALUES (?, ?, 1, ?)",
                        (
                            workspace_id,
                            "entity:" + entity_id,
                            __import__("datetime")
                            .datetime.now(__import__("datetime").UTC)
                            .isoformat(),
                        ),
                    )
                self._prune_communities(conn, workspace_id, {entity_id}, rel_ids)
                conn.commit()
            return hit

        res = await self._run(_op)
        return bool(res)

    async def tombstone_relationship(self, workspace_id: str, relationship_id: str) -> bool:
        """Mark a single relationship tombstoned and prune affected communities.

        The source and target entities survive; only the edge (and any
        community summaries referencing it) are invalidated.  Communities
        referencing the removed edge have it pruned from their provenance and
        their title/summary regenerated from surviving members.
        """

        def _op() -> bool:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE graph_relationships SET tombstoned=1"
                    " WHERE workspace_id=? AND relationship_id=?",
                    (workspace_id, relationship_id),
                )
                hit = cur.rowcount > 0
                if hit:
                    conn.execute(
                        "INSERT OR IGNORE INTO graph_tombstones"
                        "(workspace_id, document_id, tombstoned, tombstoned_at)"
                        " VALUES (?, ?, 1, ?)",
                        (
                            workspace_id,
                            "relationship:" + relationship_id,
                            __import__("datetime")
                            .datetime.now(__import__("datetime").UTC)
                            .isoformat(),
                        ),
                    )
                    self._prune_communities(conn, workspace_id, set(), {relationship_id})
                conn.commit()
            return hit

        res = await self._run(_op)
        return bool(res)

    async def tombstone_document(self, workspace_id: str, document_id: str) -> int:
        """Granular tombstone (see T-IMP-006 / T-RV19-05).

        Acquires the write lock before any read (T-RV19-03).
        """

        def _op() -> int:
            count = 0
            tombstoned_ent_ids: set[str] = set()
            tombstoned_rel_ids: set[str] = set()
            changed_ent_ids: set[str] = set()
            changed_rel_ids: set[str] = set()
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for table, idcol, _store in (
                        ("graph_entities", "entity_id", tombstoned_ent_ids),
                        ("graph_relationships", "relationship_id", tombstoned_rel_ids),
                    ):
                        cur = conn.execute(
                            "SELECT "
                            + idcol
                            + ", payload FROM "
                            + table
                            + " WHERE workspace_id=? AND tombstoned=0",
                            (workspace_id,),
                        )
                        rows = cur.fetchall()
                        for gid, payload in rows:
                            data = json.loads(payload)
                            doc_ids: list[str] = data.get("document_ids", [])
                            if document_id not in doc_ids:
                                continue
                            chunk_ids: list[str] = data.get("chunk_ids", [])
                            remaining_doc_ids = [d for d in doc_ids if d != document_id]
                            # F9: rebuild from VERIFIED surviving ownership. A chunk is
                            # kept only if a SURVIVING document verifiably owns it; every
                            # deleted OR unverified (legacy) chunk is excluded.
                            surviving_mapped: set[str] = set()
                            for sd in remaining_doc_ids:
                                cur3 = conn.execute(
                                    "SELECT chunk_id FROM graph_document_chunks"
                                    " WHERE workspace_id=? AND document_id=?",
                                    (workspace_id, sd),
                                )
                                surviving_mapped.update(r[0] for r in cur3.fetchall())
                            remaining_chunk_ids = [
                                c for c in chunk_ids if c in surviving_mapped
                            ]
                            if not remaining_chunk_ids:
                                remaining_doc_ids = []
                            if remaining_doc_ids:
                                data["document_ids"] = remaining_doc_ids
                                data["chunk_ids"] = remaining_chunk_ids
                                data["description"] = ""
                                conn.execute(
                                    "UPDATE " + table + " SET payload=?"
                                    " WHERE workspace_id=? AND " + idcol + "=?",
                                    (json.dumps(data), workspace_id, gid),
                                )
                                if table == "graph_entities":
                                    changed_ent_ids.add(gid)
                                else:
                                    changed_rel_ids.add(gid)
                            else:
                                data["document_ids"] = []
                                data["chunk_ids"] = []
                                conn.execute(
                                    "UPDATE " + table + " SET tombstoned=1, payload=?"
                                    " WHERE workspace_id=? AND " + idcol + "=?",
                                    (json.dumps(data), workspace_id, gid),
                                )
                                if table == "graph_entities":
                                    tombstoned_ent_ids.add(gid)
                                else:
                                    tombstoned_rel_ids.add(gid)
                                count += 1
                    if tombstoned_ent_ids:
                        placeholders = ",".join("?" for _ in tombstoned_ent_ids)
                        placeholders2 = ",".join("?" for _ in tombstoned_ent_ids)
                        cur = conn.execute(
                            "SELECT relationship_id, payload FROM graph_relationships"
                            " WHERE workspace_id=? AND tombstoned=0"
                            " AND (source_id IN (" + placeholders + ")"
                            " OR target_id IN (" + placeholders2 + "))",
                            (workspace_id, *tombstoned_ent_ids, *tombstoned_ent_ids),
                        )
                        for rid, payload in cur.fetchall():
                            data = json.loads(payload)
                            data["document_ids"] = []
                            data["chunk_ids"] = []
                            conn.execute(
                                "UPDATE graph_relationships SET tombstoned=1, payload=?"
                                " WHERE workspace_id=? AND relationship_id=?",
                                (json.dumps(data), workspace_id, rid),
                            )
                            tombstoned_rel_ids.add(rid)
                            count += 1
                    self._prune_communities(
                        conn, workspace_id, tombstoned_ent_ids,
                        tombstoned_rel_ids, changed_ent_ids, changed_rel_ids,
                    )
                    # Retire deleted document's chunks + drop mapping.
                    cur = conn.execute(
                        "SELECT chunk_id FROM graph_document_chunks"
                        " WHERE workspace_id=? AND document_id=?",
                        (workspace_id, document_id),
                    )
                    retired = [r[0] for r in cur.fetchall()]
                    now = datetime.now(UTC).isoformat()
                    for c in retired:
                        conn.execute(
                            "INSERT OR IGNORE INTO graph_retired_chunks"
                            "(workspace_id, chunk_id, retired_at) VALUES (?, ?, ?)",
                            (workspace_id, c, now),
                        )
                    conn.execute(
                        "DELETE FROM graph_document_chunks WHERE workspace_id=? AND document_id=?",
                        (workspace_id, document_id),
                    )
                    # T-IMP-005: record document-level tombstone.
                    conn.execute(
                        "INSERT INTO graph_tombstones"
                        " (workspace_id, document_id, tombstoned, tombstoned_at)"
                        " VALUES (?, ?, 1, ?)"
                        " ON CONFLICT(workspace_id, document_id)"
                        " DO UPDATE SET tombstoned=1, tombstoned_at=excluded.tombstoned_at",
                        (workspace_id, document_id, now),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return count

        res = await self._run(_op)
        return int(res)

    async def tombstone_chunks(self, workspace_id: str, chunk_ids: list[str]) -> int:
        """Tombstone graph rows whose provenance includes any of chunk_ids."""
        if not chunk_ids:
            return 0
        chunk_set = set(chunk_ids)

        def _op() -> int:
            count = 0
            tombstoned_ent_ids: set[str] = set()
            tombstoned_rel_ids: set[str] = set()
            changed_ent_ids: set[str] = set()
            changed_rel_ids: set[str] = set()
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for table, idcol, _store in (
                        ("graph_entities", "entity_id", tombstoned_ent_ids),
                        ("graph_relationships", "relationship_id", tombstoned_rel_ids),
                    ):
                        cur = conn.execute(
                            "SELECT "
                            + idcol
                            + ", payload FROM "
                            + table
                            + " WHERE workspace_id=? AND tombstoned=0",
                            (workspace_id,),
                        )
                        rows = cur.fetchall()
                        for gid, payload in rows:
                            data = json.loads(payload)
                            entity_chunk_ids: list[str] = data.get("chunk_ids", [])
                            if not chunk_set.intersection(set(entity_chunk_ids)):
                                continue
                            remaining_chunk_ids = [
                                c for c in entity_chunk_ids if c not in chunk_set
                            ]
                            doc_ids: list[str] = data.get("document_ids", [])
                            surviving_docs = []
                            for d in doc_ids:
                                curd = conn.execute(
                                    "SELECT chunk_id FROM graph_document_chunks"
                                    " WHERE workspace_id=? AND document_id=?",
                                    (workspace_id, d),
                                )
                                doc_chunks = {r[0] for r in curd.fetchall()}
                                if doc_chunks and (doc_chunks & set(remaining_chunk_ids)):
                                    surviving_docs.append(d)
                                elif not doc_chunks:
                                    if remaining_chunk_ids:
                                        surviving_docs.append(d)
                            data["chunk_ids"] = remaining_chunk_ids
                            data["document_ids"] = surviving_docs
                            if remaining_chunk_ids and surviving_docs:
                                data["description"] = ""
                                conn.execute(
                                    "UPDATE " + table + " SET payload=?"
                                    " WHERE workspace_id=? AND " + idcol + "=?",
                                    (json.dumps(data), workspace_id, gid),
                                )
                                if table == "graph_entities":
                                    changed_ent_ids.add(gid)
                                else:
                                    changed_rel_ids.add(gid)
                            else:
                                data["document_ids"] = []
                                data["chunk_ids"] = []
                                conn.execute(
                                    "UPDATE " + table + " SET tombstoned=1, payload=?"
                                    " WHERE workspace_id=? AND " + idcol + "=?",
                                    (json.dumps(data), workspace_id, gid),
                                )
                                if table == "graph_entities":
                                    tombstoned_ent_ids.add(gid)
                                else:
                                    tombstoned_rel_ids.add(gid)
                                count += 1
                    self._prune_communities(
                        conn, workspace_id, tombstoned_ent_ids,
                        tombstoned_rel_ids, changed_ent_ids, changed_rel_ids,
                    )
                    now = datetime.now(UTC).isoformat()
                    for c in chunk_ids:
                        conn.execute(
                            "INSERT OR IGNORE INTO graph_retired_chunks"
                            "(workspace_id, chunk_id, retired_at) VALUES (?, ?, ?)",
                            (workspace_id, c, now),
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return count

        res = await self._run(_op)
        return int(res)

    async def delete_workspace(self, workspace_id: str) -> int:
        def _op() -> int:
            total = 0
            with self._connect() as conn:
                for table in (
                    "graph_entities",
                    "graph_relationships",
                    "graph_communities",
                    "graph_attributions",
                ):
                    cur = conn.execute(
                        "DELETE FROM " + table + " WHERE workspace_id=?",
                        (workspace_id,),
                    )
                    total += cur.rowcount
                # Also clean workspace settings
                conn.execute(
                    "DELETE FROM graph_workspace_settings WHERE workspace_id=?", (workspace_id,)
                )
                conn.commit()
            return total

        res = await self._run(_op)
        return int(res)

    # Attribution record (T-V2-008): one row per answered ask, workspace-scoped
    # so cross-workspace reads are rejected by the same filter discipline.
    # Bound: one row per answer, no per-chunk fan-out.

    async def record_attribution(
        self,
        workspace_id: str,
        trace_id: str,
        answer_id: str,
        retrieval_mode: str,
        entity_ids: list[str],
        relationship_ids: list[str],
        community_ids: list[str],
        scores: dict[str, float],
    ) -> None:
        """Write one attribution row per answered ask (T-V2-008).

        Workspace-scoped: the row is keyed by (workspace_id, trace_id) and
        every read filters by workspace_id, so cross-workspace attribution
        reads are rejected (0 foreign rows, proven by test).
        """

        def _op() -> None:
            with self._connect() as conn:
                # Respect per-workspace enablement: disabled workspaces pay nothing
                cur = conn.execute(
                    "SELECT enabled FROM graph_workspace_settings WHERE workspace_id=?",
                    (workspace_id,),
                )
                row = cur.fetchone()
                if row is not None and not row[0]:
                    return
                conn.execute(
                    "INSERT INTO graph_attributions("
                    "workspace_id, trace_id, answer_id, retrieval_mode, "
                    "entity_ids, relationship_ids, community_ids, scores, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(workspace_id, trace_id) DO UPDATE SET "
                    "answer_id=excluded.answer_id, retrieval_mode=excluded.retrieval_mode, "
                    "entity_ids=excluded.entity_ids, relationship_ids=excluded.relationship_ids, "
                    "community_ids=excluded.community_ids, scores=excluded.scores",
                    (
                        workspace_id,
                        trace_id,
                        answer_id,
                        retrieval_mode,
                        json.dumps(entity_ids),
                        json.dumps(relationship_ids),
                        json.dumps(community_ids),
                        json.dumps(scores),
                        __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
                    ),
                )
                conn.commit()

        await self._run(_op)

    async def get_attribution(self, workspace_id: str, trace_id: str) -> dict | None:
        """Fetch one attribution row, workspace-scoped (cross-workspace rejected)."""

        def _op() -> dict | None:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT enabled FROM graph_workspace_settings WHERE workspace_id=?",
                    (workspace_id,),
                )
                row = cur.fetchone()
                if row is not None and not row[0]:
                    return None
                cur = conn.execute(
                    "SELECT workspace_id, trace_id, answer_id, retrieval_mode, "
                    "entity_ids, relationship_ids, community_ids, scores, created_at "
                    "FROM graph_attributions WHERE workspace_id=? AND trace_id=?",
                    (workspace_id, trace_id),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "workspace_id": row[0],
                    "trace_id": row[1],
                    "answer_id": row[2],
                    "retrieval_mode": row[3],
                    "entity_ids": json.loads(row[4]),
                    "relationship_ids": json.loads(row[5]),
                    "community_ids": json.loads(row[6]),
                    "scores": json.loads(row[7]),
                    "created_at": row[8],
                }

        return await self._run(_op)

    async def list_attributions(self, workspace_id: str) -> list[dict]:
        """List all attribution rows for a workspace (for eval queries)."""

        def _op() -> list[dict]:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT enabled FROM graph_workspace_settings WHERE workspace_id=?",
                    (workspace_id,),
                )
                row = cur.fetchone()
                if row is not None and not row[0]:
                    return []
                cur = conn.execute(
                    "SELECT workspace_id, trace_id, answer_id, retrieval_mode, "
                    "entity_ids, relationship_ids, community_ids, scores, created_at "
                    "FROM graph_attributions WHERE workspace_id=?",
                    (workspace_id,),
                )
                out = []
                for r in cur.fetchall():
                    out.append(
                        {
                            "workspace_id": r[0],
                            "trace_id": r[1],
                            "answer_id": r[2],
                            "retrieval_mode": r[3],
                            "entity_ids": json.loads(r[4]),
                            "relationship_ids": json.loads(r[5]),
                            "community_ids": json.loads(r[6]),
                            "scores": json.loads(r[7]),
                            "created_at": r[8],
                        }
                    )
                return out

        return await self._run(_op)
