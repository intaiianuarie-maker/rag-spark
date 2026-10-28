"""OpenRouter embeddings are split to the provider's per-request limit, order preserved."""

import json

import httpx
import pytest
from models.adapters.openrouter_provider import (
    DEFAULT_EMBEDDING_DIMENSION,
    EMBEDDING_BATCH_LIMIT,
    OpenRouterProvider,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_large_batch_is_split_and_keeps_order():
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        sent.append(len(inputs))
        data = [
            {"embedding": [float(int(t))] + [0.0] * (DEFAULT_EMBEDDING_DIMENSION - 1), "index": i}
            for i, t in enumerate(inputs)
        ]
        return httpx.Response(200, json={"data": data})

    provider = OpenRouterProvider(
        api_key="synthetic-fixture",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    texts = [str(i) for i in range(2 * EMBEDDING_BATCH_LIMIT + 5)]

    vectors = await provider.embed_documents(texts)

    assert sent == [EMBEDDING_BATCH_LIMIT, EMBEDDING_BATCH_LIMIT, 5]
    assert [v[0] for v in vectors] == [float(i) for i in range(len(texts))]
