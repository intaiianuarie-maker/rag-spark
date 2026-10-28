"""A stable encoder label survives transport refactoring, never identity mismatch."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from models.adapters.openrouter_provider import (
    DEFAULT_EMBEDDING_DIMENSION,
    DEFAULT_EMBEDDING_MODEL,
    OpenRouterProvider,
)
from retrieval.embedding_identity import (
    EmbeddingIdentity,
    EmbeddingIdentityError,
    provider_identity,
    verify_query_identity,
)

from scripts.rebuild_with_manifest import GeminiEmbedding2
from scripts.reindex_all_chunks import provider_identity as reindex_provider_identity

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_actual_legacy_and_current_adapters_send_the_same_encoder_payload_and_identity():
    calls = []
    texts = ["synthetic encoder identity fixture"]
    # Synthetic nonzero HTTP fixture only; never loaded into a collection.
    vector = [0.125] * DEFAULT_EMBEDDING_DIMENSION

    def handle(request):
        calls.append((request.method, str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={
            "data": [{"embedding": vector}],
            "usage": {"prompt_tokens": 5, "cost": 0.000001},
        })

    with httpx.Client(transport=httpx.MockTransport(handle)) as legacy_http:
        legacy = GeminiEmbedding2(api_key="synthetic-fixture", client=legacy_http)
        old_vectors = await legacy.embed_documents(texts)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as current_http:
        current = OpenRouterProvider(api_key="synthetic-fixture", client=current_http,
                                     base_url="https://openrouter.ai/api/v1")
        new_vectors = await current.embed_documents(texts)
    assert calls == [
        ("POST", "https://openrouter.ai/api/v1/embeddings",
         {"model": DEFAULT_EMBEDDING_MODEL, "input": texts}),
    ] * 2
    assert old_vectors == new_vectors == [vector]  # no vector transform
    old_identity = provider_identity(legacy, len(old_vectors[0]))
    new_identity = provider_identity(current, len(new_vectors[0]))
    assert old_identity == new_identity == EmbeddingIdentity(
        provider="GeminiEmbedding2", model=DEFAULT_EMBEDDING_MODEL,
        revision=DEFAULT_EMBEDDING_MODEL, dimension=3072,
    )
    assert verify_query_identity(old_identity, new_identity) == old_identity


@pytest.mark.parametrize("field,value", [
    ("provider", "UnrelatedEncoder"), ("model", "other/embedding"),
    ("revision", "other-revision"), ("dimension", 1536),
])
def test_explicit_label_never_weakens_any_of_the_four_strict_comparisons(field, value):
    requested = provider_identity(OpenRouterProvider(api_key="synthetic-fixture"), 3072)
    incompatible = replace(requested, **{field: value})
    assert not requested.matches(incompatible)
    with pytest.raises(EmbeddingIdentityError, match="incompatible"):
        verify_query_identity(incompatible, requested)


@pytest.mark.parametrize("tag", [None, "", " ", "GeminiEmbedding2 ", 7, True, [], {}])
def test_malformed_explicit_provider_labels_are_rejected_without_fallback(tag):
    provider = SimpleNamespace(model_name=DEFAULT_EMBEDDING_MODEL,
                               revision=DEFAULT_EMBEDDING_MODEL,
                               embedding_identity_provider=tag)
    with pytest.raises(EmbeddingIdentityError, match="embedding_identity_provider"):
        provider_identity(provider, 3072)


def test_adapter_without_explicit_label_keeps_its_existing_class_identity():
    class ExistingProvider:
        model_name = DEFAULT_EMBEDDING_MODEL
        revision = DEFAULT_EMBEDDING_MODEL

    assert provider_identity(ExistingProvider(), 3072).provider == "ExistingProvider"
    assert reindex_provider_identity(ExistingProvider(), 3072) == (
        provider_identity(ExistingProvider(), 3072).as_metadata()
    )


def test_reindex_helper_respects_explicit_encoder_pin_and_preserves_dict_contract():
    provider = OpenRouterProvider(api_key="synthetic-fixture")
    expected = provider_identity(provider, 3072).as_metadata()
    assert reindex_provider_identity(provider, 3072) == expected
    assert expected["embedding_provider"] == "GeminiEmbedding2"


def test_collection_labeled_with_old_transport_name_is_not_silently_aliased():
    requested = provider_identity(OpenRouterProvider(api_key="synthetic-fixture"), 3072)
    transport_named = replace(requested, provider="OpenRouterProvider")
    with pytest.raises(EmbeddingIdentityError, match="incompatible"):
        verify_query_identity(transport_named, requested)
