from __future__ import annotations

import math
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from retrieval.interfaces import Reranker as _Reranker
from retrieval.interfaces import RetrievedItem

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


#: Nemotron rerank model served through OpenRouter. The NVIDIA *free* rerank
#: endpoint explicitly excludes confidential/personal data, which is why a
#: sensitive/local-only workspace is refused at construction time.
NEMOTRON_RERANK_MODEL = "nvidia/llama-nemotron-rerank-vl-1b-v2:free"
OPENROUTER_RERANK_URL = "https://openrouter.ai/api/v1/rerank"


def _finite_float(value: Any) -> float:
    """Coerce ``value`` to a finite float or raise ``RerankAvailabilityError``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RerankAvailabilityError(f"rerank: score is not a float: {value!r}")
    score = float(value)
    if not math.isfinite(score):
        raise RerankAvailabilityError(f"rerank: score is not finite: {value!r}")
    return score


def _normalize_scores(items: list[RerankItem]) -> list[RerankItem]:
    """Min-max normalize scores into ``[0.0, 1.0]`` (best == 1.0, worst == 0.0)."""
    if not items:
        return items
    scores = [it.score for it in items]
    lo = min(scores)
    hi = max(scores)
    if hi == lo:
        # All scores equal: treat as fully relevant when positive, else zero.
        value = 1.0 if hi > 0 else 0.0
        return [RerankItem(index=it.index, score=value) for it in items]
    span = hi - lo
    return [RerankItem(index=it.index, score=(it.score - lo) / span) for it in items]


class NemotronRerankProvider:
    """Nemotron rerank via OpenRouter ``POST /api/v1/rerank``.

    Replaces the previous Cohere provider. Uses the
    ``nvidia/llama-nemotron-rerank-vl-1b-v2:free`` model; the NVIDIA free
    endpoint explicitly excludes confidential/personal data, so a
    sensitive/local-only workspace is refused at construction time (before any
    network call) -- mirroring the A01 egress refusal.

    Both synchronous (:meth:`rerank`) and asynchronous (:meth:`arerank`)
    transports are supported. Offline tests inject a callable ``transport``
    (sync) or ``async_transport`` (async) that receives the request payload and
    returns a response dict shaped like ``{"status_code": int, "json": dict}``.

    Returned indices are validated to be in-range unique integers and scores
    are validated to be finite floats; scores are min-max normalized into
    ``[0.0, 1.0]`` and items are sorted by relevance (descending).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = OPENROUTER_RERANK_URL,
        model: str = NEMOTRON_RERANK_MODEL,
        min_score: float | None = None,
        transport: Callable[[dict[str, Any]], Any] | None = None,
        async_transport: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
        sensitive: bool = False,
    ) -> None:
        if sensitive:
            # NVIDIA free rerank excludes confidential/personal data: a
            # sensitive/local-only workspace can NEVER call the cloud reranker.
            raise RerankAvailabilityError(
                "rerank refused: sensitive/local-only workspace cannot call cloud reranker"
            )
        self.api_key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = base_url
        self.model = model
        self.min_score = min_score
        self.sensitive = sensitive
        self._transport = transport
        self._async_transport = async_transport
        if not self.api_key and transport is None and async_transport is None:
            raise RerankAvailabilityError("OPENROUTER_API_KEY not configured")

    def _payload(self, *, query: str, documents: list[Any]) -> dict[str, Any]:
        return {"model": self.model, "query": query, "documents": documents}

    def _parse(
        self,
        resp: Any,
        *,
        start: float,
        n_docs: int,
        top_k: int | None,
    ) -> RerankResult:
        latency_ms = (time.monotonic() - start) * 1000.0
        if isinstance(resp, dict):
            status_code: Any = resp.get("status_code", 200)
            data: Any = resp.get("json", {}) or {}
        else:  # pragma: no cover - online path normalizes to a dict first
            status_code = getattr(resp, "status_code", 200)
            json_attr = getattr(resp, "json", None)
            data = json_attr() if callable(json_attr) else (json_attr or {})
        if isinstance(status_code, str):  # defensive coercion
            try:
                status_code = int(status_code)
            except ValueError:
                status_code = 200
        if not isinstance(data, dict):
            raise RerankAvailabilityError(
                f"rerank: malformed response (non-dict body): {data!r}"
            )
        if status_code >= 400:
            raise RerankAvailabilityError(f"rerank HTTP {status_code}: {data}")
        if "results" not in data and "data" not in data:
            raise RerankAvailabilityError(
                f"rerank: malformed response (no results/data): {data!r}"
            )
        raw_items = data.get("results") or data.get("data") or []
        if not isinstance(raw_items, list):
            raise RerankAvailabilityError(
                f"rerank: malformed results payload: {raw_items!r}"
            )
        items: list[RerankItem] = []
        seen: set[int] = set()
        for i, d in enumerate(raw_items):
            if not isinstance(d, dict):
                raise RerankAvailabilityError(f"rerank: malformed item #{i}: {d!r}")
            raw_index = d.get("index", i)
            # Reject bools (subclass of int) and non-numeric indices.
            if isinstance(raw_index, bool) or not isinstance(raw_index, (int, float)):
                raise RerankAvailabilityError(
                    f"rerank: non-integer index {raw_index!r}"
                )
            index = int(raw_index)
            if float(raw_index) != index:
                raise RerankAvailabilityError(
                    f"rerank: non-integer index {raw_index!r}"
                )
            if not (0 <= index < n_docs):
                raise RerankAvailabilityError(
                    f"rerank: index {index} out of range [0, {n_docs})"
                )
            if index in seen:
                raise RerankAvailabilityError(f"rerank: duplicate index {index}")
            seen.add(index)
            score = _finite_float(d.get("relevance_score", d.get("score", 0.0)))
            items.append(RerankItem(index=index, score=score))
        if self.min_score is not None:
            items = [it for it in items if it.score >= self.min_score]
        items.sort(key=lambda it: it.score, reverse=True)
        if top_k is not None:
            items = items[:top_k]
        items = _normalize_scores(items)
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

    def rerank(
        self,
        *,
        query: str,
        documents: list[Any],
        top_k: int | None = None,
    ) -> RerankResult:
        payload = self._payload(query=query, documents=documents)
        start = time.monotonic()
        if self._transport is not None:
            resp = self._transport(payload)
        else:  # pragma: no cover - offline tests use transport
            if httpx is None:
                raise RerankAvailabilityError("httpx unavailable")
            r = httpx.post(
                self.base_url,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10.0,
            )
            resp = {"status_code": r.status_code, "json": r.json()}
        return self._parse(resp, start=start, n_docs=len(documents), top_k=top_k)

    async def arerank(
        self,
        *,
        query: str,
        documents: list[Any],
        top_k: int | None = None,
    ) -> RerankResult:
        payload = self._payload(query=query, documents=documents)
        start = time.monotonic()
        if self._async_transport is not None:
            resp = await self._async_transport(payload)
        else:  # pragma: no cover - offline tests use async_transport
            if httpx is None:
                raise RerankAvailabilityError("httpx unavailable")
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    self.base_url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=10.0,
                )
                resp = {"status_code": r.status_code, "json": r.json()}
        return self._parse(resp, start=start, n_docs=len(documents), top_k=top_k)


class OpenRouterReranker(_Reranker):
    """Orchestrator-compatible async Reranker backed by ``NemotronRerankProvider``."""

    model_name = NEMOTRON_RERANK_MODEL

    def __init__(self, provider: NemotronRerankProvider) -> None:
        self.provider = provider

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[RetrievedItem],
        top_k: int | None = None,
    ) -> list[RetrievedItem]:
        # The cloud rerank endpoint accepts text, not RetrievedItem objects.
        docs = [str(d.content) if hasattr(d, "content") else str(d) for d in documents]
        result = await self.provider.arerank(query=query, documents=docs, top_k=top_k)
        # Map reranked indices back to the original chunk objects, preserving
        # the chunk identity (deep copy) and its metadata; only the score is
        # updated from the reranker output.
        out: list[RetrievedItem] = []
        for ri in result.items:
            if 0 <= ri.index < len(documents):
                item = documents[ri.index]
                copy = item.model_copy(deep=True) if hasattr(item, "model_copy") else item
                copy.score = float(ri.score)
                out.append(copy)
        if not out:
            # Fall back to original order if the adapter returned nothing.
            out = list(documents)
        if top_k is not None:
            out = out[:top_k]
        return out


# Deprecated backward-compatibility alias. The active rerank provider is
# NemotronRerankProvider (NVIDIA free rerank via OpenRouter); Cohere is no
# longer used as a fallback. Retained so legacy imports/seam tests that
# reference ``CohereRerankProvider`` keep resolving to the new provider.
CohereRerankProvider = NemotronRerankProvider
