"""Retrieval contracts (decisions/PD-002-retrieval-embeddings-reranker.md).

Ownership (`docs/rag/retrieval-spec.md`): we own ACL-aware orchestration, fusion,
provenance and context selection; we reuse the vector store, lexical engine,
embedding model and reranker.

Future adapters (NOT wired in M0, no dependency/model added yet):

- ``VectorStore``   -> Qdrant (Apache-2.0, ``qdrant-client``) behind this ABC
- ``LexicalStore``  -> PostgreSQL FTS (tsvector/GIN; no OpenSearch in v1)
- ``EmbeddingProvider`` -> BAAI/bge-m3 (MIT, 1024-d, RO/PT/EN);
  optional OpenAI ``text-embedding-3-*`` behind the same ABC
- ``Reranker``      -> BAAI/bge-reranker-v2-m3 (MIT);
  optional Cohere/Jina rerank APIs behind the same ABC
- ``RetrievalOrchestrator`` -> our own small ACL-aware RRF orchestrator

No qdrant-client (or any vendor) type may appear in this module's signatures.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, ClassVar

from core.auth import ACLScope
from pydantic import BaseModel, Field

# Defaults from docs/rag/retrieval-spec.md.
DEFAULT_DENSE_TOP_K = 30
DEFAULT_LEXICAL_TOP_K = 30
DEFAULT_FUSION_TOP_K = 20
DEFAULT_RERANK_TOP_K = 10
DEFAULT_CONTEXT_TOP_K = 6


class RetrievalMethod(StrEnum):
    DENSE = "dense"
    LEXICAL = "lexical"
    FUSED = "fused"
    RERANKED = "reranked"
    GRAPH = "graph"


class RetrievalMode(StrEnum):
    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"
    GRAPH = "graph"
    HYBRID_GRAPH = "hybrid_graph"


class SearchFilter(BaseModel):
    """Hard metadata filter applied by stores *before* ranking.

    The orchestrator derives this from an :class:`ACLScope`; services must
    never return rows outside these workspaces (R-004).
    """

    workspace_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] | None = None
    language: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievedItem(BaseModel):
    """One retrieved evidence unit with provenance (feeds citations)."""

    content: str
    score: float
    method: RetrievalMethod = RetrievalMethod.DENSE

    workspace_id: str | None = None
    document_id: str | None = None
    document_version_id: str | None = None
    chunk_id: str | None = None
    page: int | None = None
    section: str | None = None

    #: Per-retriever scores for the RAG debugger (e.g. {"dense": 0.71, "rrf": 0.03}).
    scores: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VectorRecord(BaseModel):
    """A dense vector plus the metadata needed to rehydrate a citation."""

    id: str
    vector: list[float]
    content: str
    workspace_id: str
    document_id: str | None = None
    document_version_id: str | None = None
    chunk_id: str | None = None
    page: int | None = None
    section: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class VectorStore(ABC):
    """Dense vector store adapter boundary (Qdrant in v1)."""

    @abstractmethod
    async def ensure_collection(
        self,
        *,
        collection: str,
        dimension: int,
        distance: str = "cosine",
        recreate: bool = False,
    ) -> None:
        """Create/validate a collection for a given embedding dimension."""
        raise NotImplementedError

    @abstractmethod
    async def upsert(self, *, collection: str, records: Sequence[VectorRecord]) -> int:
        """Insert or update vectors; returns the number written."""
        raise NotImplementedError

    @abstractmethod
    async def search(
        self,
        *,
        collection: str,
        vector: list[float],
        top_k: int,
        query_filter: SearchFilter,
    ) -> list[RetrievedItem]:
        """Nearest-neighbour search restricted by ``query_filter``."""
        raise NotImplementedError

    @abstractmethod
    async def delete(
        self,
        *,
        collection: str,
        ids: Sequence[str] | None = None,
        query_filter: SearchFilter | None = None,
    ) -> int:
        """Delete by id or filter; returns the number removed."""
        raise NotImplementedError

    async def close(self) -> None:
        """Release client resources. Default: no-op."""
        return None


class LexicalStore(ABC):
    """Lexical store adapter boundary (PostgreSQL FTS in v1)."""

    @abstractmethod
    async def index(self, *, records: Sequence[VectorRecord]) -> int:
        """Index/refresh lexical documents; returns the number written."""
        raise NotImplementedError

    @abstractmethod
    async def search(
        self,
        *,
        query: str,
        top_k: int,
        query_filter: SearchFilter,
    ) -> list[RetrievedItem]:
        """Full-text search restricted by ``query_filter``."""
        raise NotImplementedError

    @abstractmethod
    async def delete(
        self,
        *,
        ids: Sequence[str] | None = None,
        query_filter: SearchFilter | None = None,
    ) -> int:
        """Delete by id or filter; returns the number removed."""
        raise NotImplementedError


class EmbeddingProvider(ABC):
    """Embedding adapter boundary (bge-m3 by default)."""

    model_name: ClassVar[str] = ""
    dimension: ClassVar[int] = 0
    revision: ClassVar[str] = ""

    @abstractmethod
    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of documents for indexing."""
        raise NotImplementedError

    @abstractmethod
    async def embed_query(self, text: str) -> list[float]:
        """Embed a single query (some models require a query prefix)."""
        raise NotImplementedError


class Reranker(ABC):
    """Cross-encoder reranker adapter boundary (bge-reranker-v2-m3 in v1)."""

    model_name: ClassVar[str] = ""

    @abstractmethod
    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[RetrievedItem],
        top_k: int | None = None,
    ) -> list[RetrievedItem]:
        """Return ``documents`` re-scored/ordered by relevance for the debugger."""
        raise NotImplementedError


class RetrievalOrchestrator(ABC):
    """ACL-aware hybrid retrieval orchestrator (our custom product glue).

    Owns: ACL scope enforcement, dense+lexical fan-out, RRF fusion, reranker
    call, context selection and provenance/trace. Must apply ACL filters before
    ranking (R-004) and produce deterministic fusion for the debugger.
    """

    @abstractmethod
    async def retrieve(
        self,
        *,
        query: str,
        acl_scope: ACLScope,
        mode: RetrievalMode = RetrievalMode.HYBRID,
        extra_filter: SearchFilter | None = None,
        dense_top_k: int = DEFAULT_DENSE_TOP_K,
        sparse_top_k: int = DEFAULT_LEXICAL_TOP_K,
        lexical_top_k: int = DEFAULT_LEXICAL_TOP_K,
        fusion_top_k: int = DEFAULT_FUSION_TOP_K,
        rerank_top_k: int = DEFAULT_RERANK_TOP_K,
        context_top_k: int = DEFAULT_CONTEXT_TOP_K,
    ) -> list[RetrievedItem]:
        """Run the hybrid pipeline and return the final context items."""
        raise NotImplementedError
