"""Knowledge Graph package (Phase 10 — Knowledge Graph, Graph Retrieval & GraphRAG)."""

from .interfaces import (
    Entity,
    EntityType,
    GraphCommunity,
    GraphSearchResult,
    GraphStore,
    Relationship,
    RelationshipType,
)
from .resolver import EntityResolver, SimpleEntityResolver
from .retriever import GraphRetriever
from .store import MemoryGraphStore

__all__ = [
    "Entity",
    "EntityResolver",
    "EntityType",
    "GraphCommunity",
    "GraphRetriever",
    "GraphSearchResult",
    "GraphStore",
    "MemoryGraphStore",
    "Relationship",
    "RelationshipType",
    "SimpleEntityResolver",
]
