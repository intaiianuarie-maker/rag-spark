from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from retrieval.interfaces import Reranker as _Reranker

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


class RerankAvailabilityError(RuntimeError):
    """Raised when the rerank service cannot be reached or returns an error."""


@dataclass
class RerankItem:
    index: int
    score: float


@dataclass
class RerankResult:
    items: list[RerankItem]
    model: str
    latency_ms: float
    cost_usd: float
    status: str = "ok"


class CohereRerankProvider:
    """Cohere rerank via OpenRouter POST /api/v1/rerank."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://openrouter.ai/api/v1/rerank",
        model: str = "cohere/rerank-v3.5",
        min_score: float | None = None,
        transport: Callable[[dict[str, Any]], Any] | None = None,
        sensitive: bool = False,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = base_url
        self.model = model
        self.min_score = min_score
        self._transport = transport
        self.sensitive = sensitive
        if sensitive:
            # Reuse A01 egress refusal: sensitive workspaces never cloud-rerank.
            raise RerankAvailabilityError(
                "rerank refused: sensitive/local-only workspace cannot call cloud reranker"
            )
        if not self.api_key and transport is None:
            raise RerankAvailabilityError("OPENROUTER_API_KEY not configured")

    def rerank(self, *, query: str, documents: list[Any], top_k: int | None = None) -> RerankResult:
        payload = {"model": self.model, "query": query, "documents": documents}
        start = time.monotonic()
        if self._transport is not None:
            resp = self._transport(payload)
        else:  # pragma: no cover - offline tests use transport
            if httpx is None:
                raise RerankAvailabilityError("httpx unavailable")
            r = httpx.post(self.base_url, json=payload,
                           headers={"Authorization": f"Bearer {self.api_key}"}, timeout=10.0)
            resp = {"status_code": r.status_code, "json": r.json()}
        latency_ms = (time.monotonic() - start) * 1000.0
        status_code = getattr(resp, "status_code", resp.get("status_code", 200))
        if isinstance(resp, dict):
            data = resp.get("json", {})
        else:
            data = getattr(resp, "json", lambda: {})()
        if status_code >= 400:
            raise RerankAvailabilityError(f"rerank HTTP {status_code}: {data}")
        raw_items = data.get("results", []) or data.get("data", []) or []
        items = [
            RerankItem(index=int(d.get("index", i)), score=float(d.get("relevance_score", 0.0)))
            for i, d in enumerate(raw_items)
        ]
        if top_k is not None:
            items = items[:top_k]
        # OpenRouter cohere shape returns 'results' with index/relevance_score.
        cost = float(data.get("usage", {}).get("cost", 0.0) or 0.0)
        if cost == 0.0:
            cost = 0.001
        return RerankResult(
            items=items,
            model=self.model,
            latency_ms=latency_ms,
            cost_usd=cost,
            status="ok",
        )


class OpenRouterReranker(_Reranker):
    """Orchestrator-compatible Reranker using CohereRerankProvider."""

    model_name = "cohere/rerank-v3.5"

    def __init__(self, provider: CohereRerankProvider) -> None:
        self.provider = provider

    async def rerank(self, *, query, documents, top_k=None):
        from retrieval.interfaces import RetrievedItem

        docs = [str(d.content) if hasattr(d, "content") else str(d) for d in documents]
        result = self.provider.rerank(query=query, documents=docs, top_k=top_k)
        # Map indices back to items.
        out: list[RetrievedItem] = []
        for ri in result.items:
            if 0 <= ri.index < len(documents):
                item = documents[ri.index]
                copy = item.model_copy(deep=True) if hasattr(item, "model_copy") else item
                copy.score = float(ri.score)
                out.append(copy)
        if not out:
            # Fall back to original order if adapter returned nothing.
            out = list(documents)
        if top_k is not None:
            out = out[:top_k]
        return out
