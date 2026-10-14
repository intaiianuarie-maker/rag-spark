"""GraphRetriever — Local and Global GraphRAG (Sections 23 and 24).

Implements:
1. Local Graph Retrieval: query -> entity match -> 1/2-hop subgraph traversal
   -> supporting source chunk provenance.
2. Global Graph Retrieval: query -> community cluster summaries.
3. Conversion to `RetrievedItem`s so the RAG answer engine and citation verifier
   can verify claims against graph-derived evidence.
"""

from __future__ import annotations

import re
from typing import Any

from retrieval.interfaces import RetrievalMethod, RetrievedItem

from .interfaces import Entity, GraphSearchResult, GraphStore


class GraphRetriever:
    """Retrieves subgraph paths and community summaries with source chunk provenance."""

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    async def local_search(
        self,
        workspace_id: str,
        query: str,
        depth: int = 1,
        max_entities: int = 5,
    ) -> GraphSearchResult:
        """Local graph retrieval: find entities mentioned in query, traverse neighbors."""
        tokens = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]
        seed_entities: list[Entity] = []

        # Find seed entities
        for token in tokens:
            matches = await self._store.find_entities_by_name(workspace_id, token)
            for m in matches:
                if m.id not in [e.id for e in seed_entities]:
                    seed_entities.append(m)
                    if len(seed_entities) >= max_entities:
                        break
            if len(seed_entities) >= max_entities:
                break

        if not seed_entities:
            return GraphSearchResult()

        seed_ids = [e.id for e in seed_entities]
        subgraph_entities, subgraph_rels = await self._store.get_subgraph(
            workspace_id, seed_ids, depth=depth
        )

        evidence_chunk_ids: set[str] = set()
        chunk_scores: dict[str, float] = {}
        for ent in subgraph_entities:
            for chunk_id in ent.chunk_ids:
                evidence_chunk_ids.add(chunk_id)
                chunk_scores[chunk_id] = min(chunk_scores.get(chunk_id, 0.0) + 1.0, 1.0)
        for rel in subgraph_rels:
            for chunk_id in rel.chunk_ids:
                evidence_chunk_ids.add(chunk_id)
                chunk_scores[chunk_id] = min(chunk_scores.get(chunk_id, 0.0) + 1.0, 1.0)

        # Rank evidence by graph relevance (mention support), not chunk id order.
        ordered_evidence = sorted(
            evidence_chunk_ids,
            key=lambda chunk_id: (-chunk_scores.get(chunk_id, 0.0), chunk_id),
        )

        # Generate summary description of extracted knowledge graph subgraph
        rel_descriptions = [
            f"{r.source_entity_id} -[{r.type}]-> {r.target_entity_id}: {r.description}"
            if r.description
            else f"{r.source_entity_id} -[{r.type}]-> {r.target_entity_id}"
            for r in subgraph_rels
        ]
        ent_descriptions = [f"{e.name} ({e.type})" for e in subgraph_entities]

        summary = (
            f"Entities: {', '.join(ent_descriptions)}. "
            f"Relationships: {'; '.join(rel_descriptions)}"
        )

        return GraphSearchResult(
            entities=subgraph_entities,
            relationships=subgraph_rels,
            evidence_chunk_ids=ordered_evidence,
            summary=summary,
            score=1.0,
        )

    async def global_search(
        self,
        workspace_id: str,
        query: str,
        *,
        max_communities: int | None = None,
        max_global_communities: int | None = None,
        max_evidence_chunks: int | None = None,
        **kwargs: Any,
    ) -> GraphSearchResult:
        """Global graph retrieval: matches query terms against community summaries.

        Expansion is bounded (`max_communities`, `max_evidence_chunks`) so a
        community tree cannot grow without limit; evidence is ranked by the
        community term overlap that surfaced it instead of chunk id order.
        """
        if max_communities is None:
            max_communities = max_global_communities
        if max_communities is None:
            max_communities = kwargs.get("max_communities", kwargs.get("max_global_communities", 5))
        if max_evidence_chunks is None:
            max_evidence_chunks = kwargs.get("max_evidence_chunks", kwargs.get("max_chunks", 20))
        max_communities = max(1, int(max_communities))
        max_evidence_chunks = max(1, int(max_evidence_chunks))

        communities = await self._store.get_communities(workspace_id)
        if not communities:
            return GraphSearchResult()

        tokens = set(re.findall(r"\w+", query.lower()))
        scored_communities = []
        for comm in communities:
            comm_text = f"{comm.title} {comm.summary}".lower()
            overlap = sum(1 for t in tokens if t in comm_text)
            if overlap > 0:
                scored_communities.append((overlap, comm))
        scored_communities.sort(key=lambda item: (-item[0], item[1].title))
        matched_communities = [comm for _, comm in scored_communities[:max_communities]]

        chunk_scores: dict[str, float] = {}
        evidence_chunks: set[str] = set()
        per_community_entity_limit = max(1, max_evidence_chunks)
        matched_scored_communities = [
            item for item in scored_communities if item[1] in matched_communities
        ]
        for overlap, comm in matched_scored_communities:
            for eid in comm.entity_ids[:per_community_entity_limit]:
                ent = await self._store.get_entity(workspace_id, eid)
                if ent:
                    for chunk_id in ent.chunk_ids:
                        evidence_chunks.add(chunk_id)
                        chunk_scores[chunk_id] = min(
                            chunk_scores.get(chunk_id, 0.0) + float(overlap), 1.0
                        )
        ordered_evidence = sorted(
            evidence_chunks,
            key=lambda chunk_id: (-chunk_scores.get(chunk_id, 0.0), chunk_id),
        )[:max_evidence_chunks]

        summary_parts = [f"[{c.title}] {c.summary}" for c in matched_communities]
        return GraphSearchResult(
            communities=matched_communities,
            evidence_chunk_ids=ordered_evidence,
            summary="\n\n".join(summary_parts),
            score=0.9,
        )

    def to_retrieved_items(
        self,
        result: GraphSearchResult,
        workspace_id: str,
    ) -> list[RetrievedItem]:
        """Convert a GraphSearchResult into standard RetrievedItems with chunk provenance.

        The synthesized ``[Knowledge Graph Context]`` passage is a **routing
        hint**, not evidence (T-V1-022): it decides *which* chunk refs are
        candidates, and is tagged ``routing_hint`` so verification never
        grounds a citation on it. The answer path hydrates every evidence ref
        to its canonical stored passage *before* the prompt context is built
        (``answer.hydration.HydratingOrchestrator``), so the description text
        is replaced by the original chunk text — and a ref that cannot be
        hydrated never reaches the model or a citation.
        """
        items: list[RetrievedItem] = []
        if not result.summary and not result.entities:
            return items

        # Routing passage (hint only): overwritten by canonical hydration
        # before context build, and never citable on its own — see above.
        passage = f"[Knowledge Graph Context]\n{result.summary}"
        # Primary item carrying chunk IDs for citation resolution
        seen_chunks: set[str] = set()
        for chunk_id in result.evidence_chunk_ids:
            if chunk_id in seen_chunks:
                continue
            seen_chunks.add(chunk_id)
            items.append(
                RetrievedItem(
                    content=passage,
                    score=result.score,
                    method=RetrievalMethod.FUSED,
                    workspace_id=workspace_id,
                    chunk_id=chunk_id,
                    metadata={
                        "is_graph": True,
                        "routing_hint": True,
                        "entities_count": len(result.entities),
                        "relationships_count": len(result.relationships),
                    },
                )
            )

        if not items and result.summary:
            items.append(
                RetrievedItem(
                    content=passage,
                    score=result.score,
                    method=RetrievalMethod.FUSED,
                    workspace_id=workspace_id,
                    # No ref → nothing to hydrate → never citable; the hint
                    # only routes (it is dropped from the answer context).
                    metadata={"is_graph": True, "routing_hint": True},
                )
            )

        return items
