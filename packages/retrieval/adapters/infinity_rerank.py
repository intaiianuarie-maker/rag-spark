"""infinity reranker adapter (rag-fork-solution TASK-M3-004) — cross-encoder/ms-marco-MiniLM-L6-v2.

Reuse, per `rag-fork-solution/DECISIONS.md` (Reranker → cross-encoder/ms-marco-MiniLM-L6-v2 MIT,
served by infinity Apache-2.0): infinity exposes an OpenAI/Jina-style
``POST /rerank`` endpoint. This adapter speaks that HTTP API with ``httpx`` — no
vendor SDK, and no reranker/model type crosses the boundary; it returns our
``RetrievedItem`` list per the ``Reranker`` ABC.

Design notes:

- **Scores in, order out.** The server returns ``{index, relevance_score}`` per
  document; the adapter maps ``index`` back to the input item (deep copy, so the
  caller's list is never mutated), sets ``score`` and ``scores["rerank"]``, then
  sorts descending with a stable content tie-break.
- **``top_k`` is the only truncation** (``None`` = keep all).
- **Typed errors** on non-200 / non-JSON / unreachable / bad shape
  (`RerankerError`), so the orchestrator can degrade explicitly.
- **Empty input short-circuits** with no network call (contract requirement).

License: infinity Apache-2.0, cross-encoder/ms-marco-MiniLM-L6-v2 MIT (see
rag-fork-solution/REUSE_MAP.md, NOTICES.md).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

import httpx

from ..interfaces import Reranker, RetrievalMethod, RetrievedItem

#: infinity's rerank route (OpenAI/Jina-compatible shape).
RERANK_PATH = "/rerank"

DEFAULT_BASE_URL = "http://localhost:7997"
DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L6-v2"
DEFAULT_TIMEOUT_SECONDS = 30.0


class RerankerError(RuntimeError):
    """Raised when infinity is unreachable or returns an unexpected payload."""


class InfinityReranker(Reranker):
    """cross-encoder/ms-marco-MiniLM-L6-v2 cross-encoder served by infinity's ``/rerank``."""

    model_name = os.environ.get("RERANK_MODEL", DEFAULT_MODEL)

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        resolved_url = base_url or os.environ.get("INFINITY_URL") or DEFAULT_BASE_URL
        self.base_url = resolved_url.rstrip("/")
        self.api_key = api_key or os.environ.get("INFINITY_API_KEY")
        self.timeout = float(timeout or DEFAULT_TIMEOUT_SECONDS)
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"content-type": "application/json"}
            if self.api_key:
                headers["authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(timeout=self.timeout, headers=headers)
        return self._client

    async def aclose(self) -> None:
        """Close the internally-owned HTTP client (injected clients are left alone)."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[RetrievedItem],
        top_k: int | None = None,
    ) -> list[RetrievedItem]:
        materialized = list(documents)
        if not materialized:
            return []

        payload: dict[str, Any] = {
            "model": self.model_name,
            "query": query,
            "documents": [item.content for item in materialized],
            "top_n": top_k if top_k is not None else len(materialized),
        }
        url = self.base_url + RERANK_PATH
        try:
            response = await self._http().post(url, json=payload)
        except httpx.HTTPError as exc:
            raise RerankerError(
                f"infinity /rerank request failed: {type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code != 200:
            raise RerankerError(
                f"infinity /rerank returned HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise RerankerError("infinity /rerank returned a non-JSON body") from exc

        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise RerankerError("infinity /rerank returned no results list")

        scored: list[RetrievedItem] = []
        for entry in results:
            if not isinstance(entry, dict):
                raise RerankerError("infinity /rerank returned a malformed result entry")
            index = entry.get("index")
            score = entry.get("relevance_score", entry.get("score"))
            if not isinstance(index, int) or index < 0 or index >= len(materialized):
                raise RerankerError(f"infinity /rerank returned an out-of-range index: {index!r}")
            if not isinstance(score, (int, float)):
                raise RerankerError(f"infinity /rerank returned a non-numeric score: {score!r}")
            item = materialized[index].model_copy(deep=True)
            item.method = RetrievalMethod.RERANKED
            item.score = float(score)
            item.scores = {**item.scores, "rerank": float(score)}
            scored.append(item)

        scored.sort(key=lambda item: (-item.score, item.content))
        return scored if top_k is None else scored[:top_k]
