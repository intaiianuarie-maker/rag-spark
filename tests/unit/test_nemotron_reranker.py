from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from rerank.openrouter_provider import (
    NEMOTRON_RERANK_MODEL,
    OPENROUTER_RERANK_URL,
    NemotronRerankProvider,
    OpenRouterReranker,
    RerankAvailabilityError,
)

pytestmark = pytest.mark.unit


def _items() -> list[Any]:
    from retrieval.interfaces import RetrievedItem

    return [
        RetrievedItem(content="alpha", score=0.0, metadata={"id": "a"}),
        RetrievedItem(content="bravo", score=0.0, metadata={"id": "b"}),
        RetrievedItem(content="charlie", score=0.0, metadata={"id": "c"}),
    ]


def _resp(json_body: dict[str, Any]) -> dict[str, Any]:
    return {"status_code": 200, "json": json_body}


def _async_transport(
    json_body: dict[str, Any],
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def _t(payload: dict[str, Any]) -> dict[str, Any]:
        return _resp(json_body)

    return _t


async def test_correct_ranking_preserves_objects_and_metadata() -> None:
    # API returns results shuffled; output must be sorted by relevance desc.
    body = {"results": [
        {"index": 2, "relevance_score": 0.3},  # charlie
        {"index": 0, "relevance_score": 0.9},  # alpha
        {"index": 1, "relevance_score": 0.6},  # bravo
    ]}
    provider = NemotronRerankProvider(api_key="x", async_transport=_async_transport(body))
    docs = _items()
    out = await OpenRouterReranker(provider).rerank(query="q", documents=docs, top_k=None)
    # alpha(0.9)->1.0, bravo(0.6)->0.5, charlie(0.3)->0.0
    assert [d.metadata["id"] for d in out] == ["a", "b", "c"]
    assert [d.score for d in out] == pytest.approx([1.0, 0.5, 0.0])
    assert out[0].content == "alpha"
    assert out[0].metadata == {"id": "a"}
    # Chunk object preserved (deep copy) and only the score updated.
    assert out[0] is not docs[0]


async def test_top_k_truncation() -> None:
    body = {"results": [
        {"index": 0, "relevance_score": 0.1},
        {"index": 1, "relevance_score": 0.9},
        {"index": 2, "relevance_score": 0.5},
    ]}
    provider = NemotronRerankProvider(api_key="x", async_transport=_async_transport(body))
    out = await OpenRouterReranker(provider).rerank(query="q", documents=_items(), top_k=2)
    assert len(out) == 2
    assert [d.metadata["id"] for d in out] == ["b", "c"]  # 0.9, 0.5


async def test_score_normalization() -> None:
    body = {"results": [
        {"index": 0, "relevance_score": 2.0},
        {"index": 1, "relevance_score": 6.0},
        {"index": 2, "relevance_score": 4.0},
    ]}
    provider = NemotronRerankProvider(api_key="x", async_transport=_async_transport(body))
    res = await provider.arerank(query="q", documents=["a", "b", "c"])
    scores = [it.score for it in res.items]
    assert max(scores) == pytest.approx(1.0)
    assert min(scores) == pytest.approx(0.0)
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert scores == sorted(scores, reverse=True)


def test_sync_rerank_path() -> None:
    body = {"results": [
        {"index": 1, "relevance_score": 0.9},
        {"index": 0, "relevance_score": 0.1},
    ]}
    sent: list[dict[str, Any]] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return _resp(body)

    provider = NemotronRerankProvider(api_key="x", transport=transport)
    res = provider.rerank(query="q", documents=["a", "b"])
    assert [it.index for it in res.items] == [1, 0]
    assert [it.score for it in res.items] == pytest.approx([1.0, 0.0])
    assert sent[0]["model"] == NEMOTRON_RERANK_MODEL


def test_sensitive_workspace_refusal() -> None:
    with pytest.raises(RerankAvailabilityError) as exc:
        NemotronRerankProvider(api_key="x", sensitive=True)
    assert "sensitive/local-only workspace cannot call cloud reranker" in str(exc.value)


async def test_http_failure_maps_to_availability_error() -> None:
    async def transport(payload: dict[str, Any]) -> dict[str, Any]:
        return {"status_code": 429, "json": {"error": "rate limited"}}

    provider = NemotronRerankProvider(api_key="x", async_transport=transport)
    with pytest.raises(RerankAvailabilityError):
        await provider.arerank(query="q", documents=["a", "b"])


@pytest.mark.parametrize(
    "body",
    [
        {"results": [{"index": "x", "relevance_score": 0.9}]},  # non-integer index
        {"results": [{"index": 0, "relevance_score": float("nan")}]},  # non-finite score
        {"results": [{"index": 99, "relevance_score": 0.9}]},  # out-of-range (2 docs)
        {"results": [  # duplicate index
            {"index": 0, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.8},
        ]},
        {"nope": "no results/data key"},  # malformed shape
    ],
)
async def test_malformed_response_maps_to_availability_error(body: dict[str, Any]) -> None:
    provider = NemotronRerankProvider(api_key="x", async_transport=_async_transport(body))
    with pytest.raises(RerankAvailabilityError):
        await provider.arerank(query="q", documents=["a", "b"])


async def test_empty_results_falls_back_to_original_order() -> None:
    # Empty results is a valid "no rerank" signal, not a malformed response.
    provider = NemotronRerankProvider(
        api_key="x", async_transport=_async_transport({"results": []})
    )
    docs = _items()
    out = await OpenRouterReranker(provider).rerank(query="q", documents=docs)
    assert [d.metadata["id"] for d in out] == ["a", "b", "c"]


async def test_zero_cohere_calls() -> None:
    sent: list[dict[str, Any]] = []

    async def transport(payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return _resp({"results": [
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.1},
        ]})

    provider = NemotronRerankProvider(api_key="x", async_transport=transport)
    # Active provider is Nemotron, never Cohere.
    assert provider.model == NEMOTRON_RERANK_MODEL
    assert provider.base_url == OPENROUTER_RERANK_URL
    assert "cohere" not in NEMOTRON_RERANK_MODEL.lower()
    assert "cohere" not in provider.base_url.lower()
    await provider.arerank(query="q", documents=["a", "b"])
    assert sent[0]["model"] == NEMOTRON_RERANK_MODEL
    # Legacy alias resolves to the Nemotron provider (no separate Cohere code).
    from rerank.openrouter_provider import CohereRerankProvider
    assert CohereRerankProvider is NemotronRerankProvider
