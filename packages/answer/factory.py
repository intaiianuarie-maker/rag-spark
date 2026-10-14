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

from models.adapters.litellm_provider import LiteLLMProvider
from models.router_config import is_local_model, local_gateway_url

from .engine import AnswerEngine
from .hydration import CanonicalHydrator, HydratingOrchestrator
from .verification import ClaimGrounder, CrossLanguageGrounder, ExtractiveGrounder

#: Production collection the hybrid orchestrator reads/writes (single v1
#: collection). T-V1-071: the EFFECTIVE name is ``core.config.vector_collection()``,
#: so the ingest writer, the purge/cleanup paths and this reader can never drift
#: onto different collections; this constant stays the documented default.
DEFAULT_COLLECTION = "knowledge"


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


def create_orchestrator(*, collection: str | None = None):
    """Dense (Qdrant) + lexical (PG FTS) + rerank (infinity) hybrid orchestrator.

    G4 wiring: threads the optional graph retriever built by
    ``graph.factory.create_graph_retriever``. When ``GRAPH_MODE=off`` (default)
    the factory returns ``None`` and the orchestrator's graph leg no-ops, so
    retrieval behavior is byte-identical to v1.
    """
    from core.config import vector_collection
    from graph.factory import create_graph_retriever
    from ingestion.persistence import get_session_factory
    from models.adapters.openrouter_provider import OpenRouterProvider
    from retrieval.adapters.pg_fts import PostgresLexicalStore
    from retrieval.adapters.qdrant_store import QdrantVectorStore
    from retrieval.orchestrator import HybridRetrievalOrchestrator

    # T-V1-071: resolve here, not as a default argument (those freeze at import
    # time and would pin the production collection into the reader).
    collection = collection or vector_collection()
    dense = QdrantVectorStore(collection=collection)
    lexical = PostgresLexicalStore(session_factory=get_session_factory())
    # T-ARCH-04: OpenRouter gemini-embedding-2 (3072-d) default embedding provider.
    embedding = OpenRouterProvider(call_site="answer.orchestrator")
    # T-ARCH-03: Local cross-encoder cut per user order; dense-only active with disclosure.
    reranker = None
    return HybridRetrievalOrchestrator(
        dense=dense,
        embedding=embedding,
        lexical=lexical,
        collection=collection,
        reranker=reranker,
        rerank_enabled=False,
        graph=create_graph_retriever(),
    )


def build_answer_engine(
    *,
    model: str,
    collection: str | None = None,
    cross_language: bool = False,
    claim_grounder: ClaimGrounder | None = None,
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
    except Exception:
        pass

    hydrator = CanonicalHydrator(chunk_lookup)

    if claim_grounder is not None:
        grounder = claim_grounder
    elif cross_language:
        grounder = CrossLanguageGrounder()
    else:
        # Default: strict extractive grounder untouched
        grounder = ExtractiveGrounder()

    return AnswerEngine(
        orchestrator=HydratingOrchestrator(create_orchestrator(collection=collection), hydrator),
        llm=_llm_provider_for(model),
        model=model,
        # No store → None → legacy existence/scope-only verification.
        chunk_lookup=hydrator if chunk_lookup is not None else None,
        # v1-rereview Q5: ground every citation via conservative extractive
        # check (claim text must be verbatim in the chunk) — interim v1 mode.
        # T-R09: or conservative cross-language grounder when opted in.
        claim_grounder=grounder,
    )
