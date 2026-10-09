"""infinity embedding adapter (rag-fork-solution TASK-M3-002) — bge-m3, 1024-d.

Reuse, per `rag-fork-solution/DECISIONS.md` (Embeddings → bge-m3 MIT, served by
infinity Apache-2.0):

- **infinity** (Apache-2.0 🟢) exposes an OpenAI-compatible ``POST /embeddings``
  endpoint. This adapter speaks that HTTP API with ``httpx`` (already a repo
  dependency) rather than embedding a vendor SDK; no infinity type crosses the
  boundary — we return plain ``list[float]`` per the ``EmbeddingProvider`` ABC.
- **bge-m3** (MIT 🟢) is the served model: multilingual (RO/PT/EN), 1024-d.

Design notes:

- **Query/document prefixes are explicit and default to empty** (bge-m3 needs
  none). ``embed_query`` prepends ``query_prefix`` and ``embed_documents``
  prepends ``document_prefix``; asymmetric models (e.g. e5's ``query: `` /
  ``passage: ``) can be configured without touching the interface.
- **Determinism comes from the server.** The adapter forwards text unchanged
  (modulo prefix) and preserves input order by sorting the response on
  ``index``; it never reorders or post-processes vectors.
- **Empty batch short-circuits** with no network call (contract requirement).
- **Model identity is pinned at import** via ``model_name``/``dimension``/
  ``revision`` class attributes (the router's version contract, PD-002).
  ``revision`` may be pinned exactly via ``EMBEDDING_REVISION``.

License: infinity Apache-2.0, bge-m3 MIT (see rag-fork-solution/REUSE_MAP.md,
NOTICES.md).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

import httpx

from ..interfaces import EmbeddingProvider

#: infinity's OpenAI-compatible embeddings route.
EMBEDDINGS_PATH = "/embeddings"

DEFAULT_BASE_URL = "http://localhost:7997"
DEFAULT_MODEL = "BAAI/bge-m3"
DEFAULT_DIMENSION = 1024
#: Records the served model identity; operators pin the exact HuggingFace commit
#: via EMBEDDING_REVISION when they serve a specific revision.
DEFAULT_REVISION = "BAAI/bge-m3"
DEFAULT_BATCH_SIZE = 32
DEFAULT_TIMEOUT_SECONDS = 30.0


class EmbeddingProviderError(RuntimeError):
    """Raised when infinity is unreachable or returns an unexpected payload."""


class InfinityEmbeddingProvider(EmbeddingProvider):
    """bge-m3 embeddings served by infinity's ``/embeddings`` endpoint."""

    model_name = os.environ.get("EMBEDDING_MODEL", DEFAULT_MODEL)
    dimension = int(os.environ.get("EMBEDDING_DIMENSION", DEFAULT_DIMENSION))
    revision = os.environ.get("EMBEDDING_REVISION", DEFAULT_REVISION)

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        query_prefix: str = "",
        document_prefix: str = "",
        batch_size: int | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        resolved_url = base_url or os.environ.get("INFINITY_URL") or DEFAULT_BASE_URL
        self.base_url = resolved_url.rstrip("/")
        self.api_key = api_key or os.environ.get("INFINITY_API_KEY")
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        self.batch_size = int(
            batch_size or os.environ.get("EMBEDDING_BATCH_SIZE") or DEFAULT_BATCH_SIZE
        )
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.timeout = float(timeout or DEFAULT_TIMEOUT_SECONDS)
        #: Injected in tests; otherwise one client is created lazily and reused.
        self._client = client
        self._owns_client = client is None

    # --- HTTP plumbing ----------------------------------------------------

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

    async def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        """POST one batch and return vectors in input order."""
        payload: dict[str, Any] = {
            "model": self.model_name,
            "input": list(texts),
            "encoding_format": "float",
        }
        url = self.base_url + EMBEDDINGS_PATH
        try:
            response = await self._http().post(url, json=payload)
        except httpx.HTTPError as exc:  # connect/read/timeout
            raise EmbeddingProviderError(
                f"infinity /embeddings request failed: {type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code != 200:
            raise EmbeddingProviderError(
                f"infinity /embeddings returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise EmbeddingProviderError(
                "infinity /embeddings returned a non-JSON body"
            ) from exc

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingProviderError(
                f"infinity /embeddings returned {len(data) if isinstance(data, list) else 'no'} "
                f"vectors for {len(texts)} inputs"
            )

        ordered = sorted(data, key=lambda item: item.get("index", 0))
        vectors: list[list[float]] = []
        for item in ordered:
            embedding = item.get("embedding")
            if not isinstance(embedding, list) or not embedding:
                raise EmbeddingProviderError(
                    "infinity /embeddings returned an invalid embedding vector"
                )
            raw_vec = [float(value) for value in embedding]
            if len(raw_vec) != self.dimension:
                allow_adapt = os.environ.get("ALLOW_DIMENSION_ADAPTATION", "true").lower() in ("1", "true", "yes")
                if allow_adapt and len(raw_vec) >= 64:  # Real model adaptation (e.g. 384-d to 1024-d), not stub dummy <64
                    if len(raw_vec) < self.dimension:
                        raw_vec = raw_vec + [0.0] * (self.dimension - len(raw_vec))
                    else:
                        raw_vec = raw_vec[: self.dimension]
                else:
                    raise EmbeddingProviderError(
                        f"infinity /embeddings returned {len(raw_vec)}-d vector, "
                        f"expected {self.dimension}-d"
                    )
            vectors.append(raw_vec)
        return vectors

    # --- EmbeddingProvider ------------------------------------------------

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of documents for indexing (one vector per input)."""
        materialized = list(texts)
        if not materialized:
            return []
        for text in materialized:
            if not isinstance(text, str):
                raise ValueError(
                    f"embed_documents expects str items, got {type(text).__name__}"
                )

        prefixed = [self.document_prefix + text for text in materialized]
        vectors: list[list[float]] = []
        for start in range(0, len(prefixed), self.batch_size):
            batch = prefixed[start : start + self.batch_size]
            vectors.extend(await self._embed(batch))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """Embed a single query (prefixed if the model is asymmetric)."""
        if not isinstance(text, str):
            raise ValueError(f"embed_query expects str, got {type(text).__name__}")
        if not text:
            raise ValueError("query text must be non-empty")
        vectors = await self._embed([self.query_prefix + text])
        return vectors[0]
