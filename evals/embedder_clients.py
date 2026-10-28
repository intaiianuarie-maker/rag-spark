"""Embedder clients for T-EVAL-008 — one cloud, one local, one shared contract.

The point of this module is that NOTHING else has to know which embedder it is
talking to. Both legs run through the same :class:`Embedder` interface, the same
batching, the same retry policy and the same accounting, so a difference in
results cannot be blamed on the protocol. Only the endpoint and the vector
dimension differ.

  * :class:`GeminiEmbedding2` — cloud, metered, 3072-d, costs real money.
  * :class:`BgeM3Local` — the PRODUCTION embedder (BAAI/bge-m3, 1024-d, MIT).
    Shipped as inert under T-V1-029; the local infinity container was removed by
    user order. For T-EVAL-008 RESUME (user 2026-10-16, paid cloud SCOPED to this
    ticket only), BGE-M3 is served via OpenRouter using OPENROUTER_API_KEY. Cost
    is metered and reported with cost_is_metered=True for accounting transparency.
    This eval is the ONLY ticket that touches paid cloud for bge-m3; T-028 lock
    holds everywhere else.

Flakiness discipline (both cloud endpoints flaked earlier this week): a bounded
retry with backoff wraps PURE API calls only. Three attempts, then it raises
loudly. Failures are counted per endpoint and surfaced in the report — a silent
skip would be a falsified eval, so there is no code path that swallows one.
"""

from __future__ import annotations

import abc
import dataclasses
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

#: Total attempts per API call, including the first. Three is the card's bound.
MAX_ATTEMPTS = 3
#: Backoff base in seconds; attempt N waits BASE * 2**(N-1).
BACKOFF_BASE_SECONDS = 0.5

#: Cloud model ID, verbatim as pinned by the ticket. Not a variable, not
#: inferred: if this model is unavailable the eval must fail, not silently
#: substitute a different embedding model.
GEMINI_EMBEDDING_MODEL = "google/gemini-embedding-2"
GEMINI_EMBEDDING_DIMENSION = 3072

#: Production embedder identity, matching packages/retrieval/adapters.
BGE_M3_MODEL = "BAAI/bge-m3"
BGE_M3_DIMENSION = 1024
BGE_M3_URL = "http://127.0.0.1:7997"

OPENROUTER_EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"


class EmbedderError(RuntimeError):
    """Raised when an endpoint could not be used after every attempt."""


@dataclasses.dataclass(slots=True)
class EndpointStats:
    """Per-endpoint accounting. Reported, never swallowed."""

    endpoint: str
    n_calls: int = 0
    n_failed_calls: int = 0
    n_retries: int = 0
    n_vectors: int = 0
    prompt_tokens: int = 0
    cost_usd: float = 0.0
    #: Set when the endpoint reports no metering (the local leg).
    cost_is_metered: bool = True

    @property
    def failure_rate(self) -> float:
        if self.n_calls == 0:
            return 0.0
        return self.n_failed_calls / self.n_calls

    def as_row(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "calls": self.n_calls,
            "failed_calls": self.n_failed_calls,
            "retries": self.n_retries,
            "vectors": self.n_vectors,
            "prompt_tokens": self.prompt_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "cost_is_metered": self.cost_is_metered,
            "failure_rate": round(self.failure_rate, 4),
        }


def _retrying_call(
    stats: EndpointStats,
    describe: str,
    call: Callable[[], None],
) -> None:
    """Run one pure API call with bounded retries; raise loudly on exhaustion.

    Only wraps the API call itself — no parsing, no arithmetic, nothing that is
    not idempotent. Every attempt that raised increments ``n_retries`` except
    the first, and an endpoint that never succeeds increments
    ``n_failed_calls`` before the error propagates.
    """
    last: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            call()
        except Exception as exc:  # re-raised below if the retries are exhausted
            last = exc
            if attempt < MAX_ATTEMPTS:
                stats.n_retries += 1
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                continue
            stats.n_failed_calls += 1
            raise EmbedderError(
                f"{stats.endpoint}: {describe} failed after {MAX_ATTEMPTS} "
                f"attempts (last error: {type(exc).__name__}: {exc})"
            ) from exc
        else:
            return
    raise EmbedderError(f"{stats.endpoint}: {describe} exhausted retries") from last


class Embedder(abc.ABC):
    """One embedding leg. Implementations differ only in endpoint + dimension."""

    name: str
    dimension: int
    priced: bool
    stats: EndpointStats

    @abc.abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch, returning one vector per input, in input order."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release the HTTP client. Safe to call more than once."""

    def __enter__(self) -> Embedder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _check_response(payload: object, expected: int, endpoint: str) -> list[list[float]]:
    """Validate the OpenAI-shaped embeddings payload and pull out the vectors."""
    if not isinstance(payload, dict):
        raise EmbedderError(f"{endpoint}: response was {type(payload).__name__}, not JSON")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected:
        got = len(data) if isinstance(data, list) else "no 'data' list"
        raise EmbedderError(f"{endpoint}: expected {expected} embeddings, got {got}")
    vectors: list[list[float]] = []
    for item in data:
        if not isinstance(item, dict):
            raise EmbedderError(f"{endpoint}: malformed embedding item")
        raw = item.get("embedding")
        if not isinstance(raw, list) or not raw:
            raise EmbedderError(f"{endpoint}: empty embedding vector")
        vectors.append([float(x) for x in raw])
    return vectors


class GeminiEmbedding2(Embedder):
    """Cloud leg: google/gemini-embedding-2 via OpenRouter, metered."""

    name = "google/gemini-embedding-2"
    dimension = GEMINI_EMBEDDING_DIMENSION
    priced = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        url: str = OPENROUTER_EMBEDDINGS_URL,
        client: httpx.Client | None = None,
        timeout: float = 120.0,
    ) -> None:
        resolved = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not resolved:
            raise EmbedderError(
                "OPENROUTER_API_KEY is unset; the cloud leg cannot be metered "
                "or run. Refusing to skip it."
            )
        self._api_key = resolved
        self._url = url
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self.stats = EndpointStats(endpoint=self.name, cost_is_metered=True)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        batch = list(texts)
        if not batch:
            return []
        payload: dict[str, object] = {"model": GEMINI_EMBEDDING_MODEL, "input": batch}
        state: dict[str, list[list[float]]] = {}

        def call() -> None:
            response = self._client.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "content-type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            state["vectors"] = _check_response(body, len(batch), self.name)
            usage = body.get("usage") if isinstance(body, dict) else None
            if isinstance(usage, dict):
                tokens = usage.get("prompt_tokens")
                if isinstance(tokens, int):
                    self.stats.prompt_tokens += tokens
                cost = usage.get("cost")
                if isinstance(cost, (int, float)):
                    self.stats.cost_usd += float(cost)

        self.stats.n_calls += 1
        _retrying_call(self.stats, f"embedding {len(batch)} text(s)", call)
        vectors = state["vectors"]
        for vector in vectors:
            if len(vector) != self.dimension:
                raise EmbedderError(
                    f"{self.name}: expected {self.dimension}-d, got {len(vector)}-d"
                )
        self.stats.n_vectors += len(vectors)
        return vectors

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class BgeM3Local(Embedder):
    """BGE-M3 leg for T-EVAL-008: served via OpenRouter (local infinity removed).

    T-V1-029 shipped V1 inert: the local infinity container, image, and bge-m3
    weights were removed by user order. For T-EVAL-008 RESUME (user 2026-10-16),
    paid cloud use is SCOPED to this ticket only (meter + report spend; T-028
    lock holds everywhere else). BGE-M3 is routed through OpenRouter, which
    returns 1024-d vectors matching the production model identity (BAAI/bge-m3).

    Cost is metered and reported (cost_is_metered=True) so the cost table
    reflects real spend for both legs. The eval sends eval/synthetic texts only,
    never live knowledge.
    """

    name = BGE_M3_MODEL
    dimension = BGE_M3_DIMENSION
    priced = True  # metered via OpenRouter; cost reported in the cost table

    def __init__(
        self,
        *,
        base_url: str = OPENROUTER_EMBEDDINGS_URL,
        model: str = BGE_M3_MODEL,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 120.0,
        referer: str = "https://knowledge-os.local",
    ) -> None:
        self._url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self._api_key:
            raise EmbedderError(
                f"{self.name}: OPENROUTER_API_KEY not set; cannot route bge-m3 "
                f"to OpenRouter (local infinity service is removed per T-V1-029)"
            )
        self._referer = referer
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self.stats = EndpointStats(endpoint=self.name, cost_is_metered=True)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        batch = list(texts)
        if not batch:
            return []
        payload = {"model": self._model, "input": batch}
        state: dict[str, list[list[float]]] = {}
        usage_state: dict[str, int | float] = {}

        def call() -> None:
            response = self._client.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "content-type": "application/json",
                    "HTTP-Referer": self._referer,
                    "X-Title": "T-EVAL-008",
                },
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            state["vectors"] = _check_response(body, len(batch), self.name)
            usage = body.get("usage") if isinstance(body, dict) else None
            if isinstance(usage, dict):
                tokens = usage.get("prompt_tokens")
                if isinstance(tokens, int):
                    usage_state["prompt_tokens"] = tokens
                cost = usage.get("cost")
                if isinstance(cost, (int, float)):
                    usage_state["cost_usd"] = float(cost)

        self.stats.n_calls += 1
        _retrying_call(self.stats, f"embedding {len(batch)} text(s)", call)
        vectors = state["vectors"]
        for vector in vectors:
            if len(vector) != self.dimension:
                raise EmbedderError(
                    f"{self.name}: expected {self.dimension}-d, got {len(vector)}-d"
                )
        self.stats.prompt_tokens += int(usage_state.get("prompt_tokens", 0))
        self.stats.cost_usd += usage_state.get("cost_usd", 0.0)
        self.stats.n_vectors += len(vectors)
        return vectors

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


# ---------------------------------------------------------------------------
# T-ARCH-02 Single OpenRouter provider surface extension
# ---------------------------------------------------------------------------

from models.adapters.openrouter_provider import (  # noqa: E402
    OpenRouterProvider,
)

OpenRouterSurface = OpenRouterProvider


class OpenRouterChat:
    """Chat leg for evals: gemma free-first with automatic paid fallback (T-ARCH-02)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        call_site: str = "evals",
        timeout: float = 90.0,
    ) -> None:
        self.provider = OpenRouterProvider(
            api_key=api_key,
            base_url=base_url,
            sync_client=client,
            default_call_site=call_site,
            timeout=timeout,
        )
        self.stats = self.provider.stats

    def chat(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        return self.provider.chat(
            prompt,
            model=model,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.provider.embed(texts)

    def close(self) -> None:
        self.provider.close()