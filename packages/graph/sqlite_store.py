"""SQLite-backed GraphStore - V2 work package G2 (additive, default embedded).

Workspace-scoped nodes/edges/communities with delete/tombstone propagation
mirroring the Q3 lifecycle semantics. No new dependency: stdlib sqlite3 in a
worker thread via asyncio.to_thread. Neo4j stays optional/later (adapters/).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import ClassVar, TypeVar

from .interfaces import Entity, GraphCommunity, GraphStore, Relationship

T = TypeVar("T")

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
CREATE INDEX IF NOT EXISTS idx_grel_src ON graph_relationships(workspace_id, source_id);
CREATE INDEX IF NOT EXISTS idx_grel_tgt ON graph_relationships(workspace_id, target_id);
"""


def _loads_entity(row: str) -> Entity:
    return Entity.model_validate(json.loads(row))


def _loads_relationship(row: str) -> Relationship:
    return Relationship.model_validate(json.loads(row))


def _loads_community(row: str) -> GraphCommunity:
    return GraphCommunity.model_validate(json.loads(row))


def _load_entity_name(
    conn: sqlite3.Connection, workspace_id: str, entity_id: str
) -> str | None:
    """Fetch a single entity's name from the store (for summary regeneration)."""
    row = conn.execute(
        "SELECT payload FROM graph_entities"
        " WHERE workspace_id=? AND entity_id=?",
        (workspace_id, entity_id),
    ).fetchone()
    if row is None:
        return None
    return Entity.model_validate(json.loads(row[0])).name


class SQLiteGraphStore(GraphStore):
    """Embedded persistent GraphStore; one file, workspace-partitioned rows."""

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
            conn = sqlite3.connect(
                str(self._path), check_same_thread=False, timeout=5.0
            )
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
            rows = [
                (workspace_id, e.id, e.model_dump_json())
                for e in entities
                if e.workspace_id == workspace_id
            ]
            if not rows:
                return 0
            with self._connect() as conn:
                conn.executemany(
                    "INSERT INTO graph_entities(workspace_id, entity_id, payload, tombstoned)"
                    " VALUES (?, ?, ?, 0)"
                    " ON CONFLICT(workspace_id, entity_id)"
                    " DO UPDATE SET payload=excluded.payload, tombstoned=0",
                    rows,
                )
                conn.commit()
            return len(rows)

        res = await self._run(_op)
        return int(res)

    async def upsert_relationships(
        self, workspace_id: str, relationships: Sequence[Relationship]
    ) -> int:
        def _op() -> int:
            rows = [
                (workspace_id, r.id, r.source_entity_id, r.target_entity_id, r.model_dump_json())
                for r in relationships
                if r.workspace_id == workspace_id
            ]
            if not rows:
                return 0
            with self._connect() as conn:
                conn.executemany(
                    "INSERT INTO graph_relationships"
                    "(workspace_id, relationship_id, source_id, target_id, payload, tombstoned)"
                    " VALUES (?, ?, ?, ?, ?, 0)"
                    " ON CONFLICT(workspace_id, relationship_id)"
                    " DO UPDATE SET payload=excluded.payload,"
                    " source_id=excluded.source_id, target_id=excluded.target_id, tombstoned=0",
                    rows,
                )
                conn.commit()
            return len(rows)

        res = await self._run(_op)
        return int(res)

    async def get_entity(self, workspace_id: str, entity_id: str) -> Entity | None:
        def _op() -> Entity | None:
            with self._connect() as conn:
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
                    "SELECT payload FROM graph_entities"
                    " WHERE workspace_id=? AND tombstoned=0",
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
                    "SELECT payload FROM graph_entities"
                    " WHERE workspace_id=? AND tombstoned=0",
                    (workspace_id,),
                )
                entities = {e.id: e for r in cur.fetchall() for e in [_loads_entity(r[0])]}
                if entity_id not in entities:
                    return [], []
                cur = conn.execute(
                    "SELECT payload FROM graph_relationships"
                    " WHERE workspace_id=? AND tombstoned=0",
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
                conn.executemany(
                    "INSERT INTO graph_communities(workspace_id, community_id, payload)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(workspace_id, community_id)"
                    " DO UPDATE SET payload=excluded.payload, tombstoned=0",
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
                    "SELECT payload FROM graph_communities"
                    " WHERE workspace_id=? AND tombstoned=0",
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
    ) -> dict[str, int]:
        """Atomically upsert one extraction's entities/relationships/communities.

        A reader never sees a half-written extraction: all three tables are
        updated in a single transaction. If the worker dies mid-write, the
        transaction rolls back and no partial entities are visible (crash-
        durability via WAL + transaction atomicity).

        Returns counts {entities, relationships, communities}.
        """

        def _op() -> dict[str, int]:
            counts = {"entities": 0, "relationships": 0, "communities": 0}
            with self._connect() as conn:
                # BEGIN IMMEDIATE acquires the write lock up front so the
                # busy_timeout (5s) applies to the whole transaction, not per
                # statement, and readers see either all or none.
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if entities:
                        rows = [
                            (workspace_id, e.id, e.model_dump_json())
                            for e in entities
                            if e.workspace_id == workspace_id
                        ]
                        if rows:
                            conn.executemany(
                                "INSERT INTO graph_entities"
                                "(workspace_id, entity_id, payload, tombstoned)"
                                " VALUES (?, ?, ?, 0)"
                                " ON CONFLICT(workspace_id, entity_id)"
                                " DO UPDATE SET payload=excluded.payload,"
                                " tombstoned=0",
                                rows,
                            )
                            counts["entities"] = len(rows)
                    if relationships:
                        rel_rows = [
                            (
                                workspace_id,
                                r.id,
                                r.source_entity_id,
                                r.target_entity_id,
                                r.model_dump_json(),
                            )
                            for r in relationships
                            if r.workspace_id == workspace_id
                        ]
                        if rel_rows:
                            conn.executemany(
                                "INSERT INTO graph_relationships"
                                "(workspace_id, relationship_id, source_id,"
                                " target_id, payload, tombstoned)"
                                " VALUES (?, ?, ?, ?, ?, 0)"
                                " ON CONFLICT(workspace_id, relationship_id)"
                                " DO UPDATE SET payload=excluded.payload,"
                                " source_id=excluded.source_id,"
                                " target_id=excluded.target_id, tombstoned=0",
                                rel_rows,
                            )
                            counts["relationships"] = len(rel_rows)
                    if communities:
                        comm_rows = [
                            (workspace_id, c.id, c.model_dump_json())
                            for c in communities
                            if c.workspace_id == workspace_id
                        ]
                        if comm_rows:
                            conn.executemany(
                                "INSERT INTO graph_communities"
                                "(workspace_id, community_id, payload)"
                                " VALUES (?, ?, ?)"
                                " ON CONFLICT(workspace_id, community_id)"
                                " DO UPDATE SET payload=excluded.payload,"
                                " tombstoned=0",
                                comm_rows,
                            )
                            counts["communities"] = len(comm_rows)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return counts

        res = await self._run(_op)
        return res

    def _prune_communities(
        self,
        conn: sqlite3.Connection,
        workspace_id: str,
        tombstoned_entity_ids: set[str],
        tombstoned_relationship_ids: set[str],
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
        cur = conn.execute(
            "SELECT community_id, payload FROM graph_communities"
            " WHERE workspace_id=? AND tombstoned=0",
            (workspace_id,),
        )
        for cid, payload in cur.fetchall():
            comm = _loads_community(payload)
            kept_entities = [
                eid for eid in comm.entity_ids if eid not in tombstoned_entity_ids
            ]
            kept_rels = [
                rid
                for rid in comm.relationship_ids
                if rid not in tombstoned_relationship_ids
            ]
            if not kept_entities:
                conn.execute(
                    "UPDATE graph_communities SET tombstoned=1"
                    " WHERE workspace_id=? AND community_id=?",
                    (workspace_id, cid),
                )
                invalidated += 1
            elif (
                kept_entities != comm.entity_ids
                or kept_rels != comm.relationship_ids
            ):
                # Regenerate summary/title from surviving entity names so
                # deleted provenance is not retained in searchable text.
                surviving_names: list[str] = [
                    name
                    for eid in kept_entities
                    if (name := _load_entity_name(conn, workspace_id, eid)) is not None
                ]
                comm.entity_ids = kept_entities
                comm.relationship_ids = kept_rels
                if not surviving_names:
                    # No survivable provenance — invalidate rather than
                    # retaining a summary that references deleted entities.
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
                    "UPDATE graph_entities SET tombstoned=1"
                    " WHERE workspace_id=? AND entity_id=?",
                    (workspace_id, entity_id),
                )
                hit = cur.rowcount > 0
                conn.execute(
                    "UPDATE graph_relationships SET tombstoned=1"
                    " WHERE workspace_id=? AND (source_id=? OR target_id=?)",
                    (workspace_id, entity_id, entity_id),
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
                    self._prune_communities(
                        conn, workspace_id, set(), {relationship_id}
                    )
                conn.commit()
            return hit

        res = await self._run(_op)
        return bool(res)

    async def tombstone_document(self, workspace_id: str, document_id: str) -> int:
        """Tombstone every node/edge whose provenance includes document_id.

        Communities referencing the removed nodes are pruned in the same
        transaction: they lose the deleted members and are invalidated if no
        entities remain, so global search never surfaces a summary whose
        supporting provenance was deleted. Multi-document communities keep
        their surviving (still-valid) provenance.
        """

        def _op() -> int:
            count = 0
            tombstoned_ent_ids: set[str] = set()
            tombstoned_rel_ids: set[str] = set()
            with self._connect() as conn:
                for table, idcol, store in (
                    ("graph_entities", "entity_id", tombstoned_ent_ids),
                    ("graph_relationships", "relationship_id", tombstoned_rel_ids),
                ):
                    cur = conn.execute(
                        "SELECT " + idcol + ", payload FROM " + table
                        + " WHERE workspace_id=? AND tombstoned=0",
                        (workspace_id,),
                    )
                    targets = [
                        gid
                        for gid, payload in cur.fetchall()
                        if document_id in json.loads(payload).get("document_ids", [])
                    ]
                    if targets:
                        placeholders = ",".join("?" for _ in targets)
                        conn.execute(
                            "UPDATE " + table + " SET tombstoned=1"
                            " WHERE workspace_id=? AND " + idcol
                            + " IN (" + placeholders + ")",
                            (workspace_id, *targets),
                        )
                        store.update(targets)
                        count += len(targets)
                self._prune_communities(
                    conn, workspace_id, tombstoned_ent_ids, tombstoned_rel_ids
                )
                conn.commit()
            return count

        res = await self._run(_op)
        return int(res)

    async def tombstone_chunks(
        self, workspace_id: str, chunk_ids: list[str]
    ) -> int:
        """Tombstone graph rows whose provenance includes any of chunk_ids.

        Used for superseded-version parity (T-V2-003): when a document version
        is superseded, its chunks' graph rows must stop grounding citations.
        Reuses the same tombstone + community-prune path as document tombstone,
        but scoped to version-specific chunk provenance rather than the whole
        document. Idempotent: tombstoning an already-tombstoned row is a no-op.
        """

        if not chunk_ids:
            return 0
        chunk_set = set(chunk_ids)

        def _op() -> int:
            count = 0
            tombstoned_ent_ids: set[str] = set()
            tombstoned_rel_ids: set[str] = set()
            with self._connect() as conn:
                for table, idcol, store in (
                    ("graph_entities", "entity_id", tombstoned_ent_ids),
                    ("graph_relationships", "relationship_id", tombstoned_rel_ids),
                ):
                    cur = conn.execute(
                        "SELECT " + idcol + ", payload FROM " + table
                        + " WHERE workspace_id=? AND tombstoned=0",
                        (workspace_id,),
                    )
                    targets = [
                        gid
                        for gid, payload in cur.fetchall()
                        if chunk_set.intersection(
                            set(json.loads(payload).get("chunk_ids", []))
                        )
                    ]
                    if targets:
                        placeholders = ",".join("?" for _ in targets)
                        conn.execute(
                            "UPDATE " + table + " SET tombstoned=1"
                            " WHERE workspace_id=? AND " + idcol
                            + " IN (" + placeholders + ")",
                            (workspace_id, *targets),
                        )
                        store.update(targets)
                        count += len(targets)
                self._prune_communities(
                    conn, workspace_id, tombstoned_ent_ids, tombstoned_rel_ids
                )
                conn.commit()
            return count

        res = await self._run(_op)
        return int(res)

    async def delete_workspace(self, workspace_id: str) -> int:
        def _op() -> int:
            total = 0
            with self._connect() as conn:
                for table in ("graph_entities", "graph_relationships", "graph_communities"):
                    cur = conn.execute(
                        "DELETE FROM " + table + " WHERE workspace_id=?",
                        (workspace_id,),
                    )
                    total += cur.rowcount
                conn.commit()
            return total

        res = await self._run(_op)
        return int(res)
