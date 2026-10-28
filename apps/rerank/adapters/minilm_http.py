"""Client adapter for standalone MiniLM rerank sidecar (T-ARCH-06).

Reuses the existing reranker adapter seam (`retrieval.interfaces.Reranker`).
Speaks HTTP to the sidecar (`POST /rerank`, `GET /ready`, `GET /health`).
Supports deterministic in-process evaluation fallback for test harnesses
and environments where the sidecar container is not actively running.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import Any, ClassVar

import httpx
from retrieval.interfaces import Reranker, RetrievalMethod, RetrievedItem

logger = logging.getLogger("rerank_adapter")

DEFAULT_BASE_URL = "http://localhost:7998"
DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L6-v2"
DEFAULT_TIMEOUT_SECONDS = 30.0


class MiniLMRerankerError(RuntimeError):
    """Raised when the rerank sidecar fails or returns invalid responses."""


class MiniLMReranker(Reranker):
    """Cross-encoder reranker adapter for standalone MiniLM sidecar.

    Implements `retrieval.interfaces.Reranker` ABC.
    """

    model_name: ClassVar[str] = DEFAULT_MODEL

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
        in_process_fallback: bool = True,
    ) -> None:
        self.base_url = (base_url or os.environ.get("RERANK_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout or DEFAULT_TIMEOUT_SECONDS)
        self._client = client
        self._owns_client = client is None
        self.in_process_fallback = in_process_fallback
        self._local_model: Any = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={"content-type": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        """Close HTTP client if owned."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def is_ready(self) -> bool:
        """Probe /ready endpoint: readiness-as-capability."""
        url = f"{self.base_url}/ready"
        try:
            resp = await self._http().get(url)
            if resp.status_code == 200:
                data = resp.json()
                return bool(data.get("ready", False))
            return False
        except Exception:
            return False

    def _get_local_model(self) -> Any:
        if self._local_model is None:
            import torch

            from apps.rerank.app.adapters.cross_encoder import load_cross_encoder

            torch.set_num_threads(1)
            self._local_model = load_cross_encoder(self.model_name)
        return self._local_model

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[RetrievedItem],
        top_k: int | None = None,
    ) -> list[RetrievedItem]:
        """Rerank documents with query using cross-encoder MiniLM."""
        materialized = list(documents)
        if not materialized:
            return []

        # Attempt HTTP transport to sidecar
        http_success = False
        results: list[dict[str, Any]] = []

        payload: dict[str, Any] = {
            "model": self.model_name,
            "query": query,
            "documents": [item.content for item in materialized],
            "top_n": top_k if top_k is not None else len(materialized),
        }
        url = f"{self.base_url}/rerank"

        try:
            response = await self._http().post(url, json=payload)
            if response.status_code == 200:
                body = response.json()
                res_list = body.get("results")
                if isinstance(res_list, list):
                    results = res_list
                    http_success = True
            elif not self.in_process_fallback:
                raise MiniLMRerankerError(
                    f"Rerank sidecar returned HTTP {response.status_code}: {response.text[:300]}"
                )
        except Exception as exc:
            if not self.in_process_fallback:
                raise MiniLMRerankerError(f"Rerank sidecar unreachable: {exc}") from exc
            logger.debug("Sidecar HTTP unreachable (%s); using in-process MiniLM fallback", exc)

        # Fallback to in-process scoring if sidecar is offline
        if not http_success:
            local_m = self._get_local_model()
            pairs = [(query, item.content) for item in materialized]
            scores = local_m.predict(pairs)
            results = [
                {"index": idx, "relevance_score": float(sc)}
                for idx, sc in enumerate(scores)
            ]

        # Map scores to RetrievedItems per the Reranker ABC contract
        scored: list[RetrievedItem] = []
        for entry in results:
            index = entry.get("index")
            score = entry.get("relevance_score", entry.get("score"))
            if not isinstance(index, int) or index < 0 or index >= len(materialized):
                continue
            if not isinstance(score, (int, float)):
                continue

            item = materialized[index].model_copy(deep=True)
            item.method = RetrievalMethod.RERANKED
            item.score = float(score)
            item.scores = {**item.scores, "rerank": float(score)}
            scored.append(item)

        # Sort descending by relevance score; stable tie-break by content
        scored.sort(key=lambda item: (-item.score, item.content))
        return scored if top_k is None else scored[:top_k]
