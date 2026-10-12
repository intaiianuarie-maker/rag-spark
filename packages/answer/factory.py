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
from .hydration import CanonicalHydrator, HydratingOrchestrator
from .verification import ExtractiveGrounder

#: Collection the hybrid orchestrator reads/writes (single v1 collection).
DEFAULT_COLLECTION = "knowledge"

#: Model the answer engine requests from LiteLLM (M7 router owns per-workspace
#: config later).
DEFAULT_ANSWER_MODEL = "gpt-4o-mini"


def create_orchestrator(*, collection: str = DEFAULT_COLLECTION):
    """Dense (Qdrant) + lexical (PG FTS) + rerank (infinity) hybrid orchestrator.

    G4 wiring: threads the optional graph retriever built by
    ``graph.factory.create_graph_retriever``. When ``GRAPH_MODE=off`` (default)
    the factory returns ``None`` and the orchestrator's graph leg no-ops, so
    retrieval behavior is byte-identical to v1.
    """
    from graph.factory import create_graph_retriever
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
        graph=create_graph_retriever(),
    )


def build_answer_engine() -> AnswerEngine:
    """Construct the answer engine from settings (lazy; safe to call per request).

    T-V1-022 wiring: one :class:`~answer.hydration.CanonicalHydrator` is built
    over the store lookup and shared by **both** hydration seams —

    * the orchestrator is decorated with
      :class:`~answer.hydration.HydratingOrchestrator`, so every retrieved
      candidate ref is resolved to the current document version, the caller's
      scope, the original text and its provenance **before** the prompt context
      is built (retrieved summaries/payload text never override it), and
    * the same hydrator is handed to the engine as its ``chunk_lookup``, so
      :func:`answer.verification.verify_claims` bulk-hydrates every cited ref
      and revalidates lifecycle state immediately before returning the answer.

    Without a store the engine keeps the legacy ``chunk_lookup=None``
    contract and the decorator is a pass-through.
    """
    model = os.environ.get("ANSWER_MODEL", DEFAULT_ANSWER_MODEL)
    chunk_lookup = None
    try:
        from .adapters.postgres_lookup import PostgresChunkLookup

        chunk_lookup = PostgresChunkLookup()
    except Exception:
        pass

    hydrator = CanonicalHydrator(chunk_lookup)

    return AnswerEngine(
        orchestrator=HydratingOrchestrator(create_orchestrator(), hydrator),
        llm=LiteLLMProvider(),
        model=model,
        # No store → None → legacy existence/scope-only verification.
        chunk_lookup=hydrator if chunk_lookup is not None else None,
        # v1-rereview Q5: ground every citation via conservative extractive
        # check (claim text must be verbatim in the chunk) — interim v1 mode.
        claim_grounder=ExtractiveGrounder(),
    )
