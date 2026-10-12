"""Knowledge Graph & GraphRAG interfaces (Phase 10 / Section 22-24).

Implements core domain entities, relationships, communities, entity resolution,
and the GraphStore abstract contract with workspace isolation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EntityType(StrEnum):
    PERSON = "PERSON"
    ORGANIZATION = "ORGANIZATION"
    PRODUCT = "PRODUCT"
    LOCATION = "LOCATION"
    CONTRACT = "CONTRACT"
    PROJECT = "PROJECT"
    DATE = "DATE"
    CONCEPT = "CONCEPT"
    TECHNOLOGY = "TECHNOLOGY"
    EVENT = "EVENT"


class RelationshipType(StrEnum):
    WORKS_FOR = "WORKS_FOR"
    OWNS = "OWNS"
    SIGNED = "SIGNED"
    MENTIONS = "MENTIONS"
    DEPENDS_ON = "DEPENDS_ON"
    PART_OF = "PART_OF"
    RELATED_TO = "RELATED_TO"
    PRODUCES = "PRODUCES"
    LOCATED_IN = "LOCATED_IN"


class Entity(BaseModel):
    """A distinct knowledge entity extracted from documents or user domain."""

    id: str
    name: str
    type: EntityType | str = EntityType.CONCEPT
    description: str = ""
    workspace_id: str
    aliases: list[str] = Field(default_factory=list)
    chunk_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)


class Relationship(BaseModel):
    """A directed semantic relationship between two entities."""

    id: str
    source_entity_id: str
    target_entity_id: str
    type: RelationshipType | str = RelationshipType.RELATED_TO
    description: str = ""
    weight: float = 1.0
    workspace_id: str
    chunk_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphCommunity(BaseModel):
    """A cluster/community of entities for Global GraphRAG summarization."""

    id: str
    workspace_id: str
    title: str
    summary: str
    entity_ids: list[str] = Field(default_factory=list)
    relationship_ids: list[str] = Field(default_factory=list)
    rating: float = 1.0


class GraphSearchResult(BaseModel):
    """Result of local or global graph retrieval with full evidence provenance."""

    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    communities: list[GraphCommunity] = Field(default_factory=list)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    score: float = 1.0


class GraphStore(ABC):
    """Abstract interface for knowledge graph persistence and traversal."""

    @abstractmethod
    async def ensure_graph(self, workspace_id: str) -> None:
        """Initialize or verify graph storage for a workspace."""
        raise NotImplementedError

    @abstractmethod
    async def upsert_entities(self, workspace_id: str, entities: Sequence[Entity]) -> int:
        """Insert or update entities in the workspace graph; returns number written."""
        raise NotImplementedError

    @abstractmethod
    async def upsert_relationships(
        self, workspace_id: str, relationships: Sequence[Relationship]
    ) -> int:
        """Insert or update relationships in the workspace graph; returns number written."""
        raise NotImplementedError

    @abstractmethod
    async def get_entity(self, workspace_id: str, entity_id: str) -> Entity | None:
        """Fetch an entity by ID within the specified workspace."""
        raise NotImplementedError

    @abstractmethod
    async def find_entities_by_name(
        self, workspace_id: str, name: str, entity_type: str | None = None
    ) -> list[Entity]:
        """Look up entities matching a name or alias."""
        raise NotImplementedError

    @abstractmethod
    async def get_neighbors(
        self,
        workspace_id: str,
        entity_id: str,
        depth: int = 1,
        relationship_types: list[str] | None = None,
    ) -> tuple[list[Entity], list[Relationship]]:
        """Traverse outbound/inbound relationships from an entity up to depth."""
        raise NotImplementedError

    @abstractmethod
    async def get_subgraph(
        self,
        workspace_id: str,
        entity_ids: Sequence[str],
        depth: int = 1,
    ) -> tuple[list[Entity], list[Relationship]]:
        """Extract the connected subgraph for a set of seed entities."""
        raise NotImplementedError

    @abstractmethod
    async def upsert_communities(
        self, workspace_id: str, communities: Sequence[GraphCommunity]
    ) -> int:
        """Insert or update community summaries for Global GraphRAG."""
        raise NotImplementedError

    @abstractmethod
    async def get_communities(self, workspace_id: str) -> list[GraphCommunity]:
        """Fetch all communities for a workspace."""
        raise NotImplementedError

    @abstractmethod
    async def tombstone_entity(self, workspace_id: str, entity_id: str) -> bool:
        """Mark a single entity, its incident edges, and affected communities tombstoned."""
        raise NotImplementedError

    @abstractmethod
    async def tombstone_relationship(self, workspace_id: str, relationship_id: str) -> bool:
        """Mark a single relationship tombstoned and prune affected communities."""
        raise NotImplementedError

    @abstractmethod
    async def tombstone_document(self, workspace_id: str, document_id: str) -> int:
        """Tombstone every node/edge whose provenance includes document_id."""
        raise NotImplementedError

    @abstractmethod
    async def delete_workspace(self, workspace_id: str) -> int:
        """Delete all entities, relationships, and communities for a workspace."""
        raise NotImplementedError
