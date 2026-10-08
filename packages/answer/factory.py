"""Wiring factory for the answer engine (TASK-M4-002).

Builds the real orchestrator + LLM provider from settings so the API route can
construct an :class:`~answer.engine.AnswerEngine` without knowing the adapter
details. Tests inject fakes instead of calling this.

Adapter imports are **lazy** (inside the function) so that merely importing this
module — e.g. the API route at startup — never requires the optional service
clients (qdrant-client, ...). They are only needed when an engine is actually
built, i.e. when ``/ask`` is called.
"""

from __future__ import annotations

import os

from models.adapters.litellm_provider import LiteLLMProvider

from .engine import AnswerEngine

#: Collection the hybrid orchestrator reads/writes (single v1 collection).
DEFAULT_COLLECTION = "knowledge"

#: Model the answer engine requests from LiteLLM (M7 router owns per-workspace
#: config later).
DEFAULT_ANSWER_MODEL = "gpt-4o-mini"


def create_orchestrator(*, collection: str = DEFAULT_COLLECTION):
    """Dense (Qdrant) + lexical (PG FTS) + rerank (infinity) hybrid orchestrator."""
    from ingestion.persistence import get_session_factory
    from retrieval.adapters.infinity_embed import InfinityEmbeddingProvider
    from retrieval.adapters.infinity_rerank import InfinityReranker
    from retrieval.adapters.pg_fts import PostgresLexicalStore
    from retrieval.adapters.qdrant_store import QdrantVectorStore
    from retrieval.orchestrator import HybridRetrievalOrchestrator

    dense = QdrantVectorStore(collection=collection)
    lexical = PostgresLexicalStore(session_factory=get_session_factory())
    embedding = InfinityEmbeddingProvider()
    reranker = InfinityReranker()
    return HybridRetrievalOrchestrator(
        dense=dense,
        embedding=embedding,
        lexical=lexical,
        collection=collection,
        reranker=reranker,
    )


def build_answer_engine() -> AnswerEngine:
    """Construct the answer engine from settings (lazy; safe to call per request)."""
    model = os.environ.get("ANSWER_MODEL", DEFAULT_ANSWER_MODEL)
    return AnswerEngine(
        orchestrator=create_orchestrator(),
        llm=LiteLLMProvider(),
        model=model,
    )
