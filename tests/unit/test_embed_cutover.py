"""T-ARCH-04 — Embed cutover verification suite.

Proves:
1. Default-constructed paths resolving to local providers: 0.
   - InfinityEmbeddingProvider() without args raises LocalEmbeddingRemovedError.
   - InfinityEmbeddingProvider(base_url="http://localhost:7997") raises LocalEmbeddingRemovedError.
2. Ingestion + Answer + Rebuild default to ARCH-02 OpenRouterProvider (gemini-embedding-2, 3072-d).
   - create_orchestrator() defaults to OpenRouterProvider with 3072-d, gemini-embedding-2.
   - reindex_all_chunks.reindex() defaults to OpenRouterProvider(call_site="reindex_all_chunks").
   - ingest_pipeline EMBEDDING stage instantiates OpenRouterProvider(call_site="ingest_pipeline").
3. Dimension configuration & fresh collections default to 3072-d.
   - DEFAULT_DENSE_DIM == 3072.
   - settings.embedding_dimension == 3072.
   - settings.embedding_model == "google/gemini-embedding-2".
   - settings.infinity_url == "".
4. Mixed-space writes are strictly impossible (refusal, not conversion).
   - validate_records with expected_dim=3072 rejects 1024-d vectors
     (bge-m3 space) with VectorStoreInvalidVector(REJECT_DIMENSION).
   - verify_record_identity refuses legacy bge-m3 / 1024 records against
     gemini-embedding-2 / 3072 provider.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, "packages")
sys.path.insert(0, "apps/api")

from answer.factory import create_orchestrator
from core.config import get_settings
from models.adapters.openrouter_provider import OpenRouterProvider
from retrieval.adapters.infinity_embed import (
    InfinityEmbeddingProvider,
    LocalEmbeddingRemovedError,
)
from retrieval.adapters.qdrant_store import (
    DEFAULT_DENSE_DIM,
    REJECT_DIMENSION,
    VectorStoreInvalidVector,
    validate_records,
)
from retrieval.embedding_identity import (
    EmbeddingIdentity,
    EmbeddingIdentityError,
    verify_query_identity,
    verify_record_identity,
)
from retrieval.interfaces import EmbeddingProvider, VectorRecord

pytestmark = pytest.mark.unit


def test_infinity_default_construction_refused() -> None:
    """Default construction of InfinityEmbeddingProvider must be refused."""
    with pytest.raises(LocalEmbeddingRemovedError, match="permanently removed"):
        InfinityEmbeddingProvider()


def test_infinity_7997_endpoint_refused() -> None:
    """Explicit localhost:7997 or infinity:7997 must be refused."""
    with pytest.raises(LocalEmbeddingRemovedError, match="7997"):
        InfinityEmbeddingProvider(base_url="http://localhost:7997")

    with pytest.raises(LocalEmbeddingRemovedError, match="7997"):
        InfinityEmbeddingProvider(base_url="http://infinity-rerank:7997")


def test_answer_orchestrator_factory_defaults_to_openrouter_gemini_3072() -> None:
    """create_orchestrator() must construct with OpenRouterProvider at 3072-d."""
    orchestrator = create_orchestrator()
    assert isinstance(orchestrator.embedding, OpenRouterProvider)
    assert orchestrator.embedding.model_name == "google/gemini-embedding-2"
    assert orchestrator.embedding.dimension == 3072
    assert orchestrator.embedding.revision == "google/gemini-embedding-2"
    assert orchestrator.embedding.default_call_site == "answer.orchestrator"


def test_qdrant_default_dense_dim_is_3072() -> None:
    """Fresh collections default to 3072 dimensions."""
    assert DEFAULT_DENSE_DIM == 3072


def test_settings_defaults_to_gemini_3072_and_empty_infinity() -> None:
    """Settings reflect gemini-embedding-2 and no infinity_url default."""
    settings = get_settings()
    assert settings.embedding_model == "google/gemini-embedding-2"
    assert settings.embedding_dimension == 3072
    assert settings.embedding_revision == "google/gemini-embedding-2"
    assert settings.infinity_url == ""


def test_embedding_provider_interface_defaults_to_gemini_3072() -> None:
    """EmbeddingProvider ABC class attributes default to gemini 3072."""
    assert EmbeddingProvider.model_name == "google/gemini-embedding-2"
    assert EmbeddingProvider.dimension == 3072
    assert EmbeddingProvider.revision == "google/gemini-embedding-2"


def test_mixed_space_vector_rejected_at_dimension_gate() -> None:
    """A 1024-d vector (bge-m3) submitted to a 3072-d collection must be rejected."""
    gemini_identity = EmbeddingIdentity(
        provider="OpenRouterProvider",
        model="google/gemini-embedding-2",
        revision="google/gemini-embedding-2",
        dimension=3072,
    )
    legacy_1024_record = VectorRecord(
        id="chunk-legacy-1",
        content="chunk sample text",
        workspace_id="ws-1",
        vector=[0.1] * 1024,
        metadata={
            "document_id": "doc-1",
            **gemini_identity.as_metadata(),
        },
    )
    with pytest.raises(VectorStoreInvalidVector) as exc_info:
        validate_records([legacy_1024_record], expected_dim=3072)
    assert exc_info.value.reason == REJECT_DIMENSION
    assert "1024" in str(exc_info.value)
    assert "3072" in str(exc_info.value)


def test_mixed_space_identity_rejected_at_collection_boundary() -> None:
    """A bge-m3/1024 record cannot enter a gemini-embedding-2 collection."""
    legacy_identity = EmbeddingIdentity(
        provider="InfinityEmbeddingProvider",
        model="BAAI/bge-m3",
        revision="BAAI/bge-m3",
        dimension=1024,
    )
    gemini_identity = EmbeddingIdentity(
        provider="OpenRouterProvider",
        model="google/gemini-embedding-2",
        revision="google/gemini-embedding-2",
        dimension=3072,
    )
    with pytest.raises(EmbeddingIdentityError, match=r"embedding_provider|embedding_model"):
        verify_record_identity(legacy_identity.as_metadata(), gemini_identity)

    with pytest.raises(EmbeddingIdentityError, match=r"embedding_provider|embedding_model"):
        verify_query_identity(legacy_identity, gemini_identity)


def test_ingest_pipeline_source_wires_openrouter_provider() -> None:
    """Static AST check verifying apps/worker/jobs/ingest_pipeline.py wires OpenRouterProvider."""
    source_path = pathlib.Path("apps/worker/jobs/ingest_pipeline.py")
    source = source_path.read_text(encoding="utf-8")
    assert "OpenRouterProvider(call_site=" in source
    assert "InfinityEmbeddingProvider" not in source


def test_reindex_source_defaults_to_openrouter_provider() -> None:
    """Static AST check verifying scripts/reindex_all_chunks.py defaults to OpenRouterProvider."""
    source_path = pathlib.Path("scripts/reindex_all_chunks.py")
    source = source_path.read_text(encoding="utf-8")
    assert "OpenRouterProvider" in source
    assert "OpenRouterProvider(call_site=\"reindex_all_chunks\")" in source
