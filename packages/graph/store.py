"""In-memory GraphStore implementation with full workspace isolation.

Suitable for hermetic test execution, development, and self-hosted environments.
Enforces R-004 workspace boundary guarantees.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

from .interfaces import Entity, GraphCommunity, GraphStore, Relationship


class MemoryGraphStore(GraphStore):
    """In-memory graph store with isolated workspace namespaces."""

    def __init__(self) -> None:
        # workspace_id -> entity_id -> Entity
        self._entities: dict[str, dict[str, Entity]] = {}
        # workspace_id -> relationship_id -> Relationship
        self._relationships: dict[str, dict[str, Relationship]] = {}
        # workspace_id -> community_id -> GraphCommunity
        self._communities: dict[str, dict[str, GraphCommunity]] = {}

    def _ensure_ws(self, ws: str) -> None:
        if ws not in self._entities:
            self._entities[ws] = {}
        if ws not in self._relationships:
            self._relationships[ws] = {}
        if ws not in self._communities:
            self._communities[ws] = {}

    async def ensure_graph(self, workspace_id: str) -> None:
        self._ensure_ws(workspace_id)

    async def upsert_entities(self, workspace_id: str, entities: Sequence[Entity]) -> int:
        self._ensure_ws(workspace_id)
        ws_entities = self._entities[workspace_id]
        written = 0
        for ent in entities:
            if ent.workspace_id == workspace_id:
                ws_entities[ent.id] = ent
                written += 1
        return written

    async def upsert_relationships(
        self, workspace_id: str, relationships: Sequence[Relationship]
    ) -> int:
        self._ensure_ws(workspace_id)
        ws_rels = self._relationships[workspace_id]
        written = 0
        for rel in relationships:
            if rel.workspace_id == workspace_id:
                ws_rels[rel.id] = rel
                written += 1
        return written

    async def get_entity(self, workspace_id: str, entity_id: str) -> Entity | None:
        self._ensure_ws(workspace_id)
        return self._entities[workspace_id].get(entity_id)

    async def find_entities_by_name(
        self, workspace_id: str, name: str, entity_type: str | None = None
    ) -> list[Entity]:
        self._ensure_ws(workspace_id)
        q = name.strip().lower()
        matches: list[Entity] = []
        for ent in self._entities[workspace_id].values():
            if entity_type and str(ent.type).upper() != entity_type.upper():
                continue
            if ent.name.lower() == q or any(a.lower() == q for a in ent.aliases):
                matches.append(ent)
            elif q in ent.name.lower() or any(q in a.lower() for a in ent.aliases):
                matches.append(ent)
        return matches

    async def get_neighbors(
        self,
        workspace_id: str,
        entity_id: str,
        depth: int = 1,
        relationship_types: list[str] | None = None,
    ) -> tuple[list[Entity], list[Relationship]]:
        self._ensure_ws(workspace_id)
        ws_entities = self._entities[workspace_id]
        ws_rels = self._relationships[workspace_id]

        if entity_id not in ws_entities:
            return [], []

        rel_filter = set(t.upper() for t in relationship_types) if relationship_types else None

        visited_entities: set[str] = {entity_id}
        collected_rels: dict[str, Relationship] = {}
        queue: deque[tuple[str, int]] = deque([(entity_id, 0)])

        while queue:
            curr_id, curr_depth = queue.popleft()
            if curr_depth >= depth:
                continue

            for rel in ws_rels.values():
                if rel_filter and str(rel.type).upper() not in rel_filter:
                    continue

                neighbor_id: str | None = None
                if rel.source_entity_id == curr_id:
                    neighbor_id = rel.target_entity_id
                elif rel.target_entity_id == curr_id:
                    neighbor_id = rel.source_entity_id

                if neighbor_id and neighbor_id in ws_entities:
                    collected_rels[rel.id] = rel
                    if neighbor_id not in visited_entities:
                        visited_entities.add(neighbor_id)
                        queue.append((neighbor_id, curr_depth + 1))

        entities = [ws_entities[eid] for eid in visited_entities if eid in ws_entities]
        return entities, list(collected_rels.values())

    async def get_subgraph(
        self,
        workspace_id: str,
        entity_ids: Sequence[str],
        depth: int = 1,
    ) -> tuple[list[Entity], list[Relationship]]:
        self._ensure_ws(workspace_id)
        all_entities: dict[str, Entity] = {}
        all_relationships: dict[str, Relationship] = {}

        for eid in entity_ids:
            ents, rels = await self.get_neighbors(workspace_id, eid, depth=depth)
            for e in ents:
                all_entities[e.id] = e
            for r in rels:
                all_relationships[r.id] = r

        return list(all_entities.values()), list(all_relationships.values())

    async def upsert_communities(
        self, workspace_id: str, communities: Sequence[GraphCommunity]
    ) -> int:
        self._ensure_ws(workspace_id)
        written = 0
        for comm in communities:
            if comm.workspace_id == workspace_id:
                self._communities[workspace_id][comm.id] = comm
                written += 1
        return written

    async def get_communities(self, workspace_id: str) -> list[GraphCommunity]:
        self._ensure_ws(workspace_id)
        return list(self._communities[workspace_id].values())

    async def delete_workspace(self, workspace_id: str) -> int:
        total = 0
        if workspace_id in self._entities:
            total += len(self._entities[workspace_id])
            del self._entities[workspace_id]
        if workspace_id in self._relationships:
            total += len(self._relationships[workspace_id])
            del self._relationships[workspace_id]
        if workspace_id in self._communities:
            total += len(self._communities[workspace_id])
            del self._communities[workspace_id]
        return total
