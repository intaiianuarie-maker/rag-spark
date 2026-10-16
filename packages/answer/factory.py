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

import logging

from models.adapters.litellm_provider import LiteLLMProvider
from models.router_config import is_local_model, local_gateway_url

from .engine import AnswerEngine
from .hydration import CanonicalHydrator, HydratingOrchestrator
from .verification import ClaimGrounder, CrossLanguageGrounder, ExtractiveGrounder

#: Production collection the hybrid orchestrator reads/writes (single v1
#: collection). T-V1-071: the EFFECTIVE name is ``core.config.vector_collection()``,
#: so the ingest writer, the purge/cleanup paths and this reader can never drift
#: onto different collections; this constant stays the documented default.
logger = logging.getLogger("knowledge_os.answer.factory")

DEFAULT_COLLECTION = "knowledge"

def _is_egress_sensitive_value(value: object) -> bool:
    """Duck-typed extraction of a sensitive/local-only verdict from a payload."""
    if hasattr(value, "sensitive"):
        return bool(value.sensitive)
    if isinstance(value, dict):
        if "sensitive" in value:
            return bool(value.get("sensitive"))
        if "local_only" in value:
            return bool(value.get("local_only"))
    if isinstance(value, (list, tuple)):
        return any(_is_egress_sensitive_value(item) for item in value)
    return bool(value)


def _egress_sensitive(**kwargs) -> bool:
    direct = (
        kwargs.get("sensitive")
        or kwargs.get("local_only")
        or kwargs.get("workspace_sensitive")
        or kwargs.get("is_sensitive")
    )
    if direct is True:
        return True
    if direct is False:
        pass
    for key, value in kwargs.items():
        if not ("sensitive" in key or "local_only" in key or "egress" in key):
            continue
        if value is None:
            continue
        if _is_egress_sensitive_value(value):
            return True
    policy = kwargs.get("policy") or kwargs.get("egress_policy")
    if policy is not None and _is_egress_sensitive_value(policy):
        return True
    return False


def _llm_provider_for(model: str) -> LiteLLMProvider:
    """Transport seam for one resolved model (T-V1-029).

    A model the policy declares LOCAL is served over the explicitly local
    transport only: ``LOCAL_GATEWAY_URL`` (loopback default), with
    ``require_local=True`` so provider construction FAILS CLOSED when that
    endpoint is not local — the cloud default resolution (``LITELLM_URL`` /
    ``OPENROUTER_API_KEY``) is bypassed entirely, so a sensitive workspace can
    never egress even when the deployment is configured for an external
    gateway. Cloud models keep the existing default resolution untouched.
    """
    if is_local_model(model):
        return LiteLLMProvider(base_url=local_gateway_url(), require_local=True)
    return LiteLLMProvider()


def create_orchestrator(*, collection: str | None = None, **policy_kwargs):
    """Dense (Qdrant) + lexical (PG FTS) + rerank (infinity) hybrid orchestrator.

    T-A01: ``policy_kwargs`` carries the workspace egress policy. A
    sensitive/local-only workspace never builds the cloud embedding transport
    (it gets a refusing embedder), and the orchestrator is constructed with
    ``sensitive=True`` so semantic/hybrid retrieval refuses before any
    embedding request; keyword-only retrieval is unaffected.
    """
    from core.config import vector_collection
    from ingestion.persistence import get_session_factory
    from models.adapters.openrouter_provider import OpenRouterProvider
    from retrieval.adapters.pg_fts import PostgresLexicalStore
    from retrieval.adapters.qdrant_store import QdrantVectorStore
    from retrieval.orchestrator import HybridRetrievalOrchestrator

    # T-K01: the optional graph leg is construction-gated. A typo'd
    # GRAPH_MODE, an import failure, or an unusable store must degrade to the
    # v1 flag-off path with a loud log — v1 routes stay green. Never propagate
    # optional-graph construction failures into /ask or /search.
    graph_retriever = None
    try:
        from graph.factory import create_graph_retriever

        graph_retriever = create_graph_retriever()
    except Exception as exc:
        logger.warning(
            "graph_retriever_construction_failed",
            extra={
                "event": "graph_retriever_construction_failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
    sensitive = _egress_sensitive(**policy_kwargs)

    # T-V1-071: resolve here, not as a default argument (those freeze at import
    # time and would pin the production collection into the reader).
    collection = collection or vector_collection()
    dense = QdrantVectorStore(collection=collection)
    lexical = PostgresLexicalStore(session_factory=get_session_factory())
    # T-ARCH-04: OpenRouter gemini-embedding-2 (3072-d) default embedding provider.
    # T-A01: sensitive/local-only workspaces must never receive it.
    reranker = None
    rerank_enabled = False
    embedding: object
    if sensitive:
        from retrieval.orchestrator import RefusingEmbeddingProvider

        embedding = RefusingEmbeddingProvider()
    else:
        # T-ARCH-04: OpenRouter gemini-embedding-2 (3072-d) default embedding provider.
        embedding = OpenRouterProvider(call_site="answer.orchestrator")
        import os

        if os.environ.get("RERANK_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
            from rerank.openrouter_provider import (
                NemotronRerankProvider,
                OpenRouterReranker,
            )

            reranker = OpenRouterReranker(NemotronRerankProvider(sensitive=False))
            rerank_enabled = True
    return HybridRetrievalOrchestrator(
        dense=dense,
        embedding=embedding,
        lexical=lexical,
        collection=collection,
        reranker=reranker,
        rerank_enabled=rerank_enabled,
        graph=graph_retriever,
        sensitive=sensitive,
    )


def build_answer_engine(
    *,
    model: str,
    collection: str | None = None,
    cross_language: bool = False,
    claim_grounder: ClaimGrounder | None = None,
    **policy_kwargs,
) -> AnswerEngine:
    """Construct the answer engine from settings (lazy; safe to call per request).

    T-V1-028: ``model`` is a REQUIRED, explicit input — the factory holds **no**
    default model and reads no model env var. The Ask handler resolves the model
    through the policy router (workspace > permitted override > operator default)
    and passes the single selected identity in here, so a ``gpt-4o-mini`` silent
    default cannot re-enter the path. An empty ``model`` is rejected by the
    provider's typed contract, not papered over with a fallback.

    T-V1-029: the TRANSPORT follows the model's policy classification — a
    locally declared model (see :func:`models.router_config.is_local_model`)
    is built on the local gateway endpoint with ``require_local=True`` (fails
    closed at construction if that endpoint is not local), so a local-only
    workspace request can never reach an external gateway. Cloud models keep
    the default gateway resolution; this factory still holds no model default
    of its own.

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
    chunk_lookup = None
    try:
        from .adapters.postgres_lookup import PostgresChunkLookup

        chunk_lookup = PostgresChunkLookup()
    except Exception as exc:
        # T-K02: never silent. A lookup construction failure (DB down, config
        # error, import failure) logs a warning with the exception type and
        # degrades to the legacy chunk_lookup=None path — the engine still
        # builds and answers, but with existence/scope-only verification.
        logger.warning(
            "chunk_lookup_construction_failed",
            extra={
                "event": "chunk_lookup_construction_failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )

    hydrator = CanonicalHydrator(chunk_lookup)

    if claim_grounder is not None:
        grounder = claim_grounder
    elif cross_language:
        grounder = CrossLanguageGrounder()
    else:
        # Default: strict extractive grounder untouched
        grounder = ExtractiveGrounder()

    return AnswerEngine(
        orchestrator=HydratingOrchestrator(
            create_orchestrator(collection=collection, **policy_kwargs),
            hydrator,
        ),
        llm=_llm_provider_for(model),
        model=model,
        # No store → None → legacy existence/scope-only verification.
        chunk_lookup=hydrator if chunk_lookup is not None else None,
        # v1-rereview Q5: ground every citation via conservative extractive
        # check (claim text must be verbatim in the chunk) — interim v1 mode.
        # T-R09: or conservative cross-language grounder when opted in.
        claim_grounder=grounder,
    )
