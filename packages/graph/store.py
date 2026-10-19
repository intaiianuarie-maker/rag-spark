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
        # workspace_id -> set of tombstoned document ids
        self._tombstoned_docs: dict[str, set[str]] = {}
        # workspace_id -> document_id -> set of chunk ids
        self._document_chunks: dict[str, dict[str, set[str]]] = {}
        # workspace_id -> set of retired chunk ids
        self._retired_chunks: dict[str, set[str]] = {}
        # workspace_id -> set of permanently tombstoned entity ids
        self._tombstoned_entities: dict[str, set[str]] = {}
        # workspace_id -> set of permanently tombstoned relationship ids
        self._tombstoned_rels: dict[str, set[str]] = {}

    def _ensure_ws(self, ws: str) -> None:
        if ws not in self._entities:
            self._entities[ws] = {}
        if ws not in self._relationships:
            self._relationships[ws] = {}
        if ws not in self._communities:
            self._communities[ws] = {}
        if ws not in self._tombstoned_docs:
            self._tombstoned_docs[ws] = set()
        if ws not in self._document_chunks:
            self._document_chunks[ws] = {}
        if ws not in self._retired_chunks:
            self._retired_chunks[ws] = set()
        if ws not in self._tombstoned_entities:
            self._tombstoned_entities[ws] = set()
        if ws not in self._tombstoned_rels:
            self._tombstoned_rels[ws] = set()

    async def ensure_graph(self, workspace_id: str) -> None:
        self._ensure_ws(workspace_id)

    async def upsert_entities(self, workspace_id: str, entities: Sequence[Entity]) -> int:
        self._ensure_ws(workspace_id)
        ws_entities = self._entities[workspace_id]
        written = 0
        for ent in entities:
            if ent.workspace_id != workspace_id:
                continue
            # Fence: skip if any of the entity's documents are tombstoned.
            if ent.id in self._tombstoned_entities[workspace_id]:
                continue
            if any(d in self._tombstoned_docs[workspace_id] for d in ent.document_ids):
                continue
            if any(c in self._retired_chunks[workspace_id] for c in ent.chunk_ids):
                continue
            # F2/F8: record document->chunk contributions only when unambiguous
            if len(ent.document_ids) == 1:
                d = ent.document_ids[0]
                self._document_chunks[workspace_id].setdefault(d, set()).update(ent.chunk_ids)
            if ent.id in ws_entities and ws_entities[ent.id].workspace_id == workspace_id:
                existing = ws_entities[ent.id]
                merged_doc_ids = list(existing.document_ids) + [
                    d for d in ent.document_ids if d not in existing.document_ids
                ]
                merged_chunk_ids = list(existing.chunk_ids) + [
                    c for c in ent.chunk_ids if c not in existing.chunk_ids
                ]
                existing.document_ids = merged_doc_ids
                existing.chunk_ids = merged_chunk_ids
                existing.name = ent.name
                existing.type = ent.type
                existing.description = ent.description
                existing.aliases = ent.aliases
                existing.properties = ent.properties
                ws_entities[ent.id] = existing
            else:
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
            if rel.workspace_id != workspace_id:
                continue
            if rel.id in self._tombstoned_rels[workspace_id]:
                continue
            if any(d in self._tombstoned_docs[workspace_id] for d in rel.document_ids):
                continue
            if any(c in self._retired_chunks[workspace_id] for c in rel.chunk_ids):
                continue
            if len(rel.document_ids) == 1:
                d = rel.document_ids[0]
                self._document_chunks[workspace_id].setdefault(d, set()).update(rel.chunk_ids)
            if rel.id in ws_rels and ws_rels[rel.id].workspace_id == workspace_id:
                existing = ws_rels[rel.id]
                merged_doc_ids = list(existing.document_ids) + [
                    d for d in rel.document_ids if d not in existing.document_ids
                ]
                merged_chunk_ids = list(existing.chunk_ids) + [
                    c for c in rel.chunk_ids if c not in existing.chunk_ids
                ]
                existing.document_ids = merged_doc_ids
                existing.chunk_ids = merged_chunk_ids
                existing.source_entity_id = rel.source_entity_id
                existing.target_entity_id = rel.target_entity_id
                existing.type = rel.type
                existing.description = rel.description
                existing.weight = rel.weight
                existing.properties = rel.properties
                ws_rels[rel.id] = existing
            else:
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

    async def tombstone_entity(self, workspace_id: str, entity_id: str) -> bool:
        """Mark an entity, its incident edges, and affected communities tombstoned.

        Mirrors the SQLite adapter's behavior: the entity and its incident
        relationships are removed, and communities referencing them are pruned
        in-place (summaries regenerated from surviving members) or tombstoned
        when no entities remain.
        """
        self._ensure_ws(workspace_id)
        if entity_id not in self._entities[workspace_id]:
            return False

        ws_rels = self._relationships[workspace_id]
        incident_rel_ids = {
            rid
            for rid, rel in ws_rels.items()
            if rel.source_entity_id == entity_id or rel.target_entity_id == entity_id
        }
        del self._entities[workspace_id][entity_id]
        for rid in incident_rel_ids:
            del ws_rels[rid]
        self._tombstoned_entities[workspace_id].add(entity_id)

        self._prune_communities(workspace_id, {entity_id}, incident_rel_ids)
        return True

    async def tombstone_relationship(self, workspace_id: str, relationship_id: str) -> bool:
        """Mark a relationship tombstoned and prune affected communities.

        The source and target entities survive; only the edge and any
        community summaries referencing it are invalidated.
        """
        self._ensure_ws(workspace_id)
        if relationship_id not in self._relationships[workspace_id]:
            return False
        del self._relationships[workspace_id][relationship_id]
        self._tombstoned_rels[workspace_id].add(relationship_id)
        self._prune_communities(workspace_id, set(), {relationship_id})
        return True

    def _prune_communities(
        self,
        workspace_id: str,
        tombstoned_entity_ids: set[str],
        tombstoned_relationship_ids: set[str],
        changed_entity_ids: set[str] | None = None,
        changed_relationship_ids: set[str] | None = None,
    ) -> None:
        """Prune invalidated members from community provenance in-place.

        Mirrors the SQLite adapter's ``_prune_communities``: communities lose
        tombstoned members; summaries/titles are regenerated from surviving
        entity names; communities with no surviving entities are tombstoned
        (soft-deleted) so global retrieval never returns deleted facts.
        """
        if changed_entity_ids is None:
            changed_entity_ids = set()
        if changed_relationship_ids is None:
            changed_relationship_ids = set()
        affected = tombstoned_entity_ids | changed_entity_ids
        affected_rels = tombstoned_relationship_ids | changed_relationship_ids
        ws_comms = self._communities[workspace_id]
        ws_entities = self._entities[workspace_id]
        for cid in list(ws_comms.keys()):
            comm = ws_comms[cid]
            kept_entities = [eid for eid in comm.entity_ids if eid not in tombstoned_entity_ids]
            kept_rels = [
                rid for rid in comm.relationship_ids if rid not in tombstoned_relationship_ids
            ]
            if not kept_entities:
                del ws_comms[cid]
                continue
            affected_member = (
                bool(set(comm.entity_ids) & affected)
                or bool(set(comm.relationship_ids) & affected_rels)
            )
            if not affected_member:
                comm.entity_ids = kept_entities
                comm.relationship_ids = kept_rels
                continue
            surviving_names = [ws_entities[eid].name for eid in kept_entities if eid in ws_entities]
            comm.entity_ids = kept_entities
            comm.relationship_ids = kept_rels
            if surviving_names:
                comm.title = " + ".join(surviving_names)
                comm.summary = "; ".join(surviving_names)
            else:
                del ws_comms[cid]

    async def tombstone_document(self, workspace_id: str, document_id: str) -> int:
        self._ensure_ws(workspace_id)
        self._tombstoned_docs[workspace_id].add(document_id)
        deleted_chunks = self._document_chunks[workspace_id].get(document_id, set())
        count = 0
        tombstoned_ent_ids: set[str] = set()
        tombstoned_rel_ids: set[str] = set()
        changed_ent_ids: set[str] = set()
        changed_rel_ids: set[str] = set()

        for eid in list(self._entities[workspace_id].keys()):
            ent = self._entities[workspace_id][eid]
            if document_id not in ent.document_ids:
                continue
            remaining_docs = [d for d in ent.document_ids if d != document_id]
            surviving_mapped = set()
            for sd in remaining_docs:
                surviving_mapped.update(self._document_chunks[workspace_id].get(sd, set()))
            has_mapping = bool(self._document_chunks[workspace_id])
            if not has_mapping:
                remaining_docs = []
            if deleted_chunks:
                remaining_chunks = [c for c in ent.chunk_ids if c not in deleted_chunks]
            elif has_mapping and surviving_mapped:
                remaining_chunks = [c for c in ent.chunk_ids if c in surviving_mapped]
            else:
                remaining_chunks = []
            if remaining_docs:
                ent.document_ids = remaining_docs
                ent.chunk_ids = remaining_chunks
                ent.description = ""
                self._entities[workspace_id][eid] = ent
                changed_ent_ids.add(eid)
            else:
                tombstoned_ent_ids.add(eid)
                del self._entities[workspace_id][eid]
                count += 1

        for rid in list(self._relationships[workspace_id].keys()):
            rel = self._relationships[workspace_id][rid]
            if document_id not in rel.document_ids:
                continue
            remaining_docs = [d for d in rel.document_ids if d != document_id]
            dangling = (
                rel.source_entity_id in tombstoned_ent_ids
                or rel.target_entity_id in tombstoned_ent_ids
            )
            surviving_mapped = set()
            for sd in remaining_docs:
                surviving_mapped.update(self._document_chunks[workspace_id].get(sd, set()))
            has_mapping = bool(self._document_chunks[workspace_id])
            if not has_mapping:
                remaining_docs = []
            if deleted_chunks:
                remaining_chunks = [c for c in rel.chunk_ids if c not in deleted_chunks]
            elif has_mapping and surviving_mapped:
                remaining_chunks = [c for c in rel.chunk_ids if c in surviving_mapped]
            else:
                remaining_chunks = []
            if remaining_docs and not dangling:
                rel.document_ids = remaining_docs
                rel.chunk_ids = remaining_chunks
                rel.description = ""
                self._relationships[workspace_id][rid] = rel
                changed_rel_ids.add(rid)
            else:
                tombstoned_rel_ids.add(rid)
                del self._relationships[workspace_id][rid]
                count += 1

        self._prune_communities(
            workspace_id, tombstoned_ent_ids, tombstoned_rel_ids,
            changed_ent_ids, changed_rel_ids,
        )
        # cleanup mapping + retire chunks
        deleted_chunks = self._document_chunks[workspace_id].get(document_id, set())
        self._retired_chunks[workspace_id].update(deleted_chunks)
        self._document_chunks[workspace_id].pop(document_id, None)
        return count

    async def is_document_tombstoned(self, workspace_id: str, document_id: str) -> bool:
        self._ensure_ws(workspace_id)
        return document_id in self._tombstoned_docs[workspace_id]

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
