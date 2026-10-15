from __future__ import annotations

import pytest

from packages.rerank.openrouter_provider import (
    CohereRerankProvider,
    RerankAvailabilityError,
)


def test_sensitive_refusal() -> None:
    with pytest.raises(RerankAvailabilityError):
        CohereRerankProvider(api_key="x",
                            transport=lambda p: {}, sensitive=True)


def test_availability_failure() -> None:
    def transport(p):
        return {"status_code": 429, "json": {"error": "rate limited"}}

    prov = CohereRerankProvider(api_key="x", transport=transport)
    with pytest.raises(RerankAvailabilityError):
        prov.rerank("q", ["a", "b"])


def test_offline_fields() -> None:
    def transport(p):
        return {"status_code": 200, "json": {"results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.1},
        ]}}

    prov = CohereRerankProvider(api_key="x", transport=transport)
    res = prov.rerank("q", ["a", "b"])
    assert res.latency_ms >= 0.0
    assert res.cost_usd > 0
    assert len(res.items) == 2
