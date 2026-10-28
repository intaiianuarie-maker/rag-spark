from __future__ import annotations

from typing import Any

import pytest
from rerank.openrouter_provider import (
    NEMOTRON_RERANK_MODEL,
    NemotronRerankProvider,
    RerankAvailabilityError,
)


def test_sensitive_refusal() -> None:
    with pytest.raises(RerankAvailabilityError) as exc:
        NemotronRerankProvider(api_key="x", sensitive=True)
    assert "sensitive/local-only workspace cannot call cloud reranker" in str(exc.value)


def test_missing_api_key_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RerankAvailabilityError):
        NemotronRerankProvider()


def test_availability_failure() -> None:
    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        return {"status_code": 429, "json": {"error": "rate limited"}}

    prov = NemotronRerankProvider(api_key="x", transport=transport)
    with pytest.raises(RerankAvailabilityError):
        prov.rerank(query="q", documents=["a", "b"])


def test_offline_fields() -> None:
    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["model"] == NEMOTRON_RERANK_MODEL
        return {"status_code": 200, "json": {"results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.1},
        ]}}

    prov = NemotronRerankProvider(api_key="x", transport=transport)
    res = prov.rerank(query="q", documents=["a", "b"])
    assert res.latency_ms >= 0.0
    assert res.cost_usd > 0
    assert res.model == NEMOTRON_RERANK_MODEL
    # Sorted by relevance (desc) and min-max normalized to [0.0, 1.0].
    assert [it.index for it in res.items] == [1, 0]
    assert [it.score for it in res.items] == pytest.approx([1.0, 0.0])


def test_top_k_truncation() -> None:
    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        return {"status_code": 200, "json": {"results": [
            {"index": 0, "relevance_score": 0.1},
            {"index": 1, "relevance_score": 0.9},
            {"index": 2, "relevance_score": 0.5},
        ]}}

    prov = NemotronRerankProvider(api_key="x", transport=transport)
    res = prov.rerank(query="q", documents=["a", "b", "c"], top_k=2)
    assert len(res.items) == 2
    assert [it.index for it in res.items] == [1, 2]


async def test_async_rerank() -> None:
    async def transport(payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["model"] == NEMOTRON_RERANK_MODEL
        return {"status_code": 200, "json": {"results": [
            {"index": 0, "relevance_score": 0.2},
            {"index": 1, "relevance_score": 0.8},
        ]}}

    prov = NemotronRerankProvider(api_key="x", async_transport=transport)
    res = await prov.arerank(query="q", documents=["a", "b"])
    assert [it.index for it in res.items] == [1, 0]
    assert [it.score for it in res.items] == pytest.approx([1.0, 0.0])

