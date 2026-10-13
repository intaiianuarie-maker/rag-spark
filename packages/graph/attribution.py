"""Graph reuse attribution record (T-V2-008).

One row per answered ask, workspace-scoped, bounded (no per-chunk fan-out).
Provenance-complete: entity/relationship/community ids + scores + retrieval mode.

Stored in the same SQLite file as the graph store (single file, one WAL),
so cross-workspace reads are rejected by the same `WHERE workspace_id=?` filter.
"""

from __future__ import annotations

from typing import Any

from retrieval.interfaces import RetrievedItem


def extract_graph_attribution(
    items: list[RetrievedItem],
    retrieval_mode: str,
) -> dict[str, Any] | None:
    """Extract graph attribution from retrieved items, if any.

    Returns None when no graph-served item is present (no row to write).
    When graph items are present, returns a dict with entity/relationship/
    community ids and scores, bounded to one row per answer.
    """
    # Collect graph items: those with is_graph or graph score
    graph_items = [
        item for item in items if (item.metadata or {}).get("is_graph") or "graph" in (item.scores or {})  # noqa: E501
    ]
    if not graph_items:
        return None

    # Collect provenance from graph items' chunk provenance and scores
    # For this ticket, we attribute at the answer level, not per-chunk:
    # one row per answer, with lists of graph element ids that served it.
    # The graph retriever's items carry chunk_ids, but the community/entity
    # provenance is in the graph store, not in the RetrievedItem. For now,
    # we attribute the chunk_ids that were graph-served, plus the retrieval
    # mode and the graph scores.
    # Future G-eval tickets will join against the stored rows to compute
    # "what fraction of answers touched graph elements" without recomputing.

    # Collect provenance: entity/relationship/community ids from retriever's
    # metadata (added in T-V2-008), plus chunk-level scores.
    entity_ids: set[str] = set()
    relationship_ids: set[str] = set()
    community_ids: set[str] = set()
    scores: dict[str, float] = {}
    chunk_ids: list[str] = []
    for item in graph_items:
        meta = item.metadata or {}
        for eid in meta.get("graph_entity_ids", []) or []:
            entity_ids.add(str(eid))
        for rid in meta.get("graph_relationship_ids", []) or []:
            relationship_ids.add(str(rid))
        for cid in meta.get("graph_community_ids", []) or []:
            community_ids.add(str(cid))
        if item.chunk_id:
            chunk_ids.append(item.chunk_id)
            scores[item.chunk_id] = (
                item.scores.get("graph", item.score) if item.scores else item.score
            )

    # If no entity/relationship/community ids were exposed (legacy items),
    # fall back to chunk_ids as entity-level provenance
    if not entity_ids and not relationship_ids and not community_ids:
        for cid in chunk_ids:
            entity_ids.add(cid)
        deduped_entity_ids = sorted(entity_ids)
        return {
            "retrieval_mode": retrieval_mode,
            "entity_ids": deduped_entity_ids,
            "relationship_ids": [],
            "community_ids": [],
            "scores": scores,
        }

    return {
        "retrieval_mode": retrieval_mode,
        "entity_ids": sorted(entity_ids),
        "relationship_ids": sorted(relationship_ids),
        "community_ids": sorted(community_ids),
        "scores": scores,
    }


async def record_attribution(
    workspace_id: str,
    trace_id: str,
    answer_id: str,
    retrieval_mode: str,
    items: list[RetrievedItem],
) -> None:
    """Write one attribution row per answered ask, if graph-served.

    Bounded: one row per answer, no per-chunk fan-out. Workspace-scoped.
    """
    attribution = extract_graph_attribution(items, retrieval_mode)
    if attribution is None:
        return

    # Use the graph store's attribution table
    from graph.factory import graph_db_path
    from graph.sqlite_store import SQLiteGraphStore

    store = SQLiteGraphStore(graph_db_path())
    try:
        await store.record_attribution(
            workspace_id=workspace_id,
            trace_id=trace_id,
            answer_id=answer_id,
            retrieval_mode=attribution["retrieval_mode"],
            entity_ids=attribution["entity_ids"],
            relationship_ids=attribution["relationship_ids"],
            community_ids=attribution["community_ids"],
            scores=attribution["scores"],
        )
    finally:
        store.close()
