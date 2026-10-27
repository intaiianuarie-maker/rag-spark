"""Single OpenRouter provider surface — chat completions and embeddings (T-ARCH-02).

Contract and policy guarantees:
1. ONE surface: all OpenRouter interactions (chat and embeddings, sync and async)
   flow through this surface.
2. Chat: ``google/gemma-4-26b-a4b-it:free`` FIRST with AUTOMATIC paid fallback to
   ``google/gemma-4-26b-a4b-it`` on pool exhaustion (pre-authorized wave-wide).
3. Embeddings: ``google/gemini-embedding-2`` (3072-d, metered).
4. Paid Gemini-generation stays LOCKED: any request for paid Gemini generation
   raises :class:`ProviderPolicyError` before network contact.
5. Accounting: per-call accounting per call site (prompt tokens, completion tokens,
   total tokens, spend in USD, free/paid counts, retries, failures). Zero accounting gaps.
6. Abort lines: bounded retries (3 attempts with exponential backoff); loud
   exhaustion errors (:class:`ProviderExhaustionError`, :class:`EmbedderError`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import threading
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

import httpx
from retrieval.interfaces import EmbeddingProvider

from ..interfaces import (
    ChatMessage,
    GenerationRequest,
    GenerationResult,
    LLMProvider,
)
from ..router_config import LOCAL_ONLY_SENTINEL, is_local_endpoint
from ..usage import StreamUsage, stream_usage_ctx
from .openrouter_transport import (
    OpenRouterTransport,
)

# ---------------------------------------------------------------------------
# Constants & Model Pins
# ---------------------------------------------------------------------------

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_BASE_URL = "http://localhost:4000"
DEFAULT_TIMEOUT = 90.0

DEFAULT_CHAT_FREE_MODEL = "google/gemma-4-26b-a4b-it:free"
DEFAULT_CHAT_PAID_MODEL = "google/gemma-4-26b-a4b-it"

DEFAULT_EMBEDDING_MODEL = "google/gemini-embedding-2"
DEFAULT_EMBEDDING_DIMENSION = 3072

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5

# Published rates in USD per token (as of 2026-10)
PAID_PRICING: dict[str, dict[str, float]] = {
    "google/gemma-4-26b-a4b-it": {
        "prompt": 0.00000009,  # $0.09 / 1M
        "completion": 0.00000030,  # $0.30 / 1M
    },
    "google/gemini-embedding-2": {
        "prompt": 0.00000015,  # $0.15 / 1M
        "completion": 0.0,
    },
}

EXHAUSTION_HTTP_CODES = frozenset({402, 429, 503})
EXHAUSTION_MARKERS = (
    "exhausted",
    "no endpoints",
    "no available endpoints",
    "provider returned error",
    "rate limit",
    "rate-limited",
    "429",
    "402",
    "insufficient",
    "temporarily unavailable",
    "overloaded",
    "quota",
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OpenRouterError(RuntimeError):
    """Base error for OpenRouter provider failures."""


class LLMProviderError(OpenRouterError):
    """Typed error for gateway / LLM provider failures."""


class ProviderPolicyError(LLMProviderError):
    """Raised when a request violates security or model locking policy."""


class ProviderExhaustionError(LLMProviderError):
    """Raised when retries or failover paths are exhausted loudly."""


class EmbedderError(OpenRouterError):
    """Typed error for embedding endpoint failures."""


# ---------------------------------------------------------------------------
# Policy Helpers
# ---------------------------------------------------------------------------


def _guard_model_name(model: str) -> str:
    name = model.strip()
    if not name:
        raise LLMProviderError("model name must not be empty")
    if name == LOCAL_ONLY_SENTINEL:
        raise LLMProviderError(
            "local-only is a policy label, not a model; refusing to send it to a gateway"
        )
    return name


def _is_paid_gemini_generation(model: str) -> bool:
    """Returns True if the model is a paid Gemini generation model."""
    lowered = model.lower().strip()
    if "gemini" not in lowered:
        return False
    if lowered.endswith(":free"):
        return False
    return True


def _guard_generation_model(model: str) -> None:
    lowered = model.lower().strip()
    if lowered == DEFAULT_EMBEDDING_MODEL.lower() or "embedding" in lowered:
        raise ProviderPolicyError(f"Embedding model '{model}' cannot be used for text generation")
    if _is_paid_gemini_generation(model):
        raise ProviderPolicyError(
            f"Paid gemini-generation is locked by policy; generators must use gemma free-first "
            f"(requested '{model}')"
        )


def _is_exhausted(status_code: int, response_text: str) -> bool:
    if status_code in EXHAUSTION_HTTP_CODES:
        return True
    lowered = response_text.lower()
    return any(marker in lowered for marker in EXHAUSTION_MARKERS)


def compute_cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    openrouter_cost: float | None = None,
) -> float:
    if openrouter_cost is not None and openrouter_cost > 0.0:
        return float(openrouter_cost)
    if model.endswith(":free"):
        return 0.0
    rates = PAID_PRICING.get(model)
    if rates:
        cost = prompt_tokens * rates["prompt"] + completion_tokens * rates["completion"]
        return round(cost, 8)
    return 0.0


# ---------------------------------------------------------------------------
# Accounting Registry
# ---------------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class CallStats:
    call_site: str
    n_calls: int = 0
    n_failed_calls: int = 0
    n_retries: int = 0
    n_free_calls: int = 0
    n_paid_calls: int = 0
    n_fallbacks: int = 0
    n_vectors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost_is_metered: bool = True
    n_unreported_usage_calls: int = 0

    @property
    def failure_rate(self) -> float:
        return (self.n_failed_calls / self.n_calls) if self.n_calls > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_site": self.call_site,
            "calls": self.n_calls,
            "failed_calls": self.n_failed_calls,
            "retries": self.n_retries,
            "free_calls": self.n_free_calls,
            "paid_calls": self.n_paid_calls,
            "fallbacks": self.n_fallbacks,
            "vectors": self.n_vectors,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "cost_is_metered": self.cost_is_metered,
            "unreported_usage_calls": self.n_unreported_usage_calls,
            "failure_rate": round(self.failure_rate, 4),
        }

    def as_row(self) -> dict[str, Any]:
        """Compatibility with evals/embedder_clients EndpointStats.as_row."""
        return {
            "endpoint": self.call_site,
            "calls": self.n_calls,
            "failed_calls": self.n_failed_calls,
            "retries": self.n_retries,
            "vectors": self.n_vectors,
            "prompt_tokens": self.prompt_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "cost_is_metered": self.cost_is_metered,
            "failure_rate": round(self.failure_rate, 4),
        }


class AccountingRegistry:
    """Thread-safe registry for per-call-site metering."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sites: dict[str, CallStats] = {}

    def get_stats(self, call_site: str = "default") -> CallStats:
        with self._lock:
            if call_site not in self._sites:
                self._sites[call_site] = CallStats(call_site=call_site)
            return self._sites[call_site]

    def record_call(
        self,
        *,
        call_site: str = "default",
        model: str = "",
        prompt_tokens: int | None = 0,
        completion_tokens: int | None = 0,
        cost_usd: float | None = 0.0,
        retries: int = 0,
        failed: bool = False,
        fallback: bool = False,
        vectors: int = 0,
    ) -> None:
        with self._lock:
            if call_site not in self._sites:
                self._sites[call_site] = CallStats(call_site=call_site)
            stats = self._sites[call_site]
            stats.n_calls += 1
            if failed:
                stats.n_failed_calls += 1
            stats.n_retries += retries
            if fallback:
                stats.n_fallbacks += 1
            if model.endswith(":free"):
                stats.n_free_calls += 1
            elif model:
                stats.n_paid_calls += 1
            stats.n_vectors += vectors
            if prompt_tokens is None or completion_tokens is None:
                stats.n_unreported_usage_calls += 1
            if prompt_tokens is not None:
                stats.prompt_tokens += prompt_tokens
                stats.total_tokens += prompt_tokens
            if completion_tokens is not None:
                stats.completion_tokens += completion_tokens
                stats.total_tokens += completion_tokens
            if cost_usd is None:
                stats.cost_is_metered = False
            else:
                stats.cost_usd += cost_usd

    def record_failure(self, call_site: str = "default", retries: int = 0) -> None:
        with self._lock:
            if call_site not in self._sites:
                self._sites[call_site] = CallStats(call_site=call_site)
            stats = self._sites[call_site]
            stats.n_calls += 1
            stats.n_failed_calls += 1
            stats.n_retries += retries

    def summary(self) -> dict[str, Any]:
        with self._lock:
            total = CallStats(call_site="TOTAL")
            sites_out: dict[str, dict[str, Any]] = {}
            for site, s in self._sites.items():
                sites_out[site] = s.as_dict()
                total.n_calls += s.n_calls
                total.n_failed_calls += s.n_failed_calls
                total.n_retries += s.n_retries
                total.n_free_calls += s.n_free_calls
                total.n_paid_calls += s.n_paid_calls
                total.n_fallbacks += s.n_fallbacks
                total.n_vectors += s.n_vectors
                total.prompt_tokens += s.prompt_tokens
                total.completion_tokens += s.completion_tokens
                total.total_tokens += s.total_tokens
                total.cost_usd += s.cost_usd
                total.n_unreported_usage_calls += s.n_unreported_usage_calls
                total.cost_is_metered = total.cost_is_metered and s.cost_is_metered
            return {
                "total": total.as_dict(),
                "sites": sites_out,
            }

    def reset(self, call_site: str | None = None) -> None:
        with self._lock:
            if call_site is None:
                self._sites.clear()
            elif call_site in self._sites:
                del self._sites[call_site]


GLOBAL_ACCOUNTING = AccountingRegistry()


# ---------------------------------------------------------------------------
# Formatting & Response Validation Helpers
# ---------------------------------------------------------------------------


def _to_openai_messages(msgs: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in msgs:
        d: dict[str, Any] = {"role": m.role.value, "content": m.content}
        if m.name:
            d["name"] = m.name
        if m.tool_call_id:
            d["tool_call_id"] = m.tool_call_id
        out.append(d)
    return out


def _trim_metadata(
    data: dict[str, Any], accounting_meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "raw": {
            "id": data.get("id"),
            "created": data.get("created"),
            "model": data.get("model"),
            "system_fingerprint": data.get("system_fingerprint"),
        }
    }
    if accounting_meta:
        meta["accounting"] = accounting_meta
    return meta


def _check_embedding_response(payload: object, expected: int, endpoint: str) -> list[list[float]]:
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


# ---------------------------------------------------------------------------
# Unified OpenRouter Surface
# ---------------------------------------------------------------------------


class OpenRouterProvider(LLMProvider, EmbeddingProvider):
    """Unified OpenRouter provider surface — chat generation and embeddings."""

    provider_name: str = "openrouter"
    model_name: ClassVar[str] = DEFAULT_EMBEDDING_MODEL
    dimension: ClassVar[int] = DEFAULT_EMBEDDING_DIMENSION
    revision: ClassVar[str] = DEFAULT_EMBEDDING_MODEL
    # Stable encoder identity, independent of the chat/HTTP adapter's Python
    # class name. scripts/rebuild_with_manifest.GeminiEmbedding2 used the same
    # OpenRouter /embeddings model + input payload and 3072-d output, with no
    # task/pooling/vector transform. Keep its persisted label, not an alias in
    # the strict identity comparison (model/revision/dimension stay mandatory).
    embedding_identity_provider: ClassVar[str] = "GeminiEmbedding2"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        sync_client: httpx.Client | None = None,
        require_local: bool = False,
        default_call_site: str = "default",
        call_site: str | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_base_seconds: float = BACKOFF_BASE_SECONDS,
        allow_paid_fallback: bool = True,
    ) -> None:
        env_openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        if base_url:
            resolved_url = base_url
        elif env_openrouter_key:
            resolved_url = DEFAULT_OPENROUTER_BASE_URL
        else:
            resolved_url = os.environ.get("LITELLM_URL", DEFAULT_BASE_URL)

        self.base_url = resolved_url.rstrip("/")
        self.api_key = (
            api_key
            or os.environ.get("LITELLM_API_KEY")
            or (env_openrouter_key if "openrouter.ai" in self.base_url else None)
            or (os.environ.get("DEEPSEEK_API_KEY") if "deepseek.com" in self.base_url else None)
        )
        self.timeout = timeout
        self.default_call_site = call_site or default_call_site
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds
        self.allow_paid_fallback = allow_paid_fallback

        if require_local and not is_local_endpoint(self.base_url):
            raise LLMProviderError(
                "local transport required but the configured gateway endpoint is not local"
            )

        self._client = client
        self._owns_client = client is None

        self._sync_client = sync_client
        self._owns_sync_client = sync_client is None

        self.transport = OpenRouterTransport(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_attempts,
            backoff_base_seconds=self.backoff_base_seconds,
            client=self._client,
            sync_client=self._sync_client,
        )

    @property
    def stats(self) -> CallStats:
        """Endpoint stats for the default call site."""
        return GLOBAL_ACCOUNTING.get_stats(self.default_call_site)

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"content-type": "application/json"}
        if self.api_key:
            h["authorization"] = f"Bearer {self.api_key}"
        if "openrouter.ai" in self.base_url:
            h["HTTP-Referer"] = "https://knowledge-os.local"
            h["X-Title"] = "Knowledge OS"
        return h

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, headers=self._headers())
        return self._client

    def _http_sync(self) -> httpx.Client:
        if self._sync_client is None:
            self._sync_client = httpx.Client(timeout=self.timeout, headers=self._headers())
        return self._sync_client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def close(self) -> None:
        if self._sync_client is not None and self._owns_sync_client:
            self._sync_client.close()
            self._sync_client = None

    def __enter__(self) -> OpenRouterProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -----------------------------------------------------------------------
    # LLMProvider interface: generate
    # -----------------------------------------------------------------------

    async def generate(
        self,
        request: GenerationRequest,
        *,
        call_site: str | None = None,
    ) -> GenerationResult:
        site = call_site or getattr(request, "call_site", None) or self.default_call_site
        _guard_model_name(request.model)
        req_model = request.model.strip()
        _guard_generation_model(req_model)

        is_gemma = "gemma" in req_model.lower()
        if is_gemma:
            primary_model = DEFAULT_CHAT_FREE_MODEL
            fallback_model = DEFAULT_CHAT_PAID_MODEL if self.allow_paid_fallback else None
        else:
            primary_model = req_model
            fallback_model = None

        client = self._http()
        url = f"{self.base_url}/chat/completions"

        def _build_payload(m: str) -> dict[str, Any]:
            p: dict[str, Any] = {
                "model": m,
                "messages": _to_openai_messages(request.messages),
                "stream": False,
            }
            if request.temperature is not None:
                p["temperature"] = request.temperature
            if request.max_tokens is not None:
                p["max_tokens"] = request.max_tokens
            if request.stop:
                p["stop"] = request.stop
            return p

        retries_used = 0
        fallback_triggered = False
        active_model = primary_model
        data: dict[str, Any] | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = await client.post(
                    url,
                    json=_build_payload(active_model),
                    headers=self._headers(),
                )
                if (
                    resp.status_code in (429, 402)
                    and active_model == primary_model
                    and fallback_model is not None
                ):
                    fallback_triggered = True
                    active_model = fallback_model
                    break
                resp.raise_for_status()
                data = resp.json()
                break
            except (httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError) as e:
                status_code = getattr(getattr(e, "response", None), "status_code", 0)
                resp_text = getattr(getattr(e, "response", None), "text", "")
                if (
                    active_model == primary_model
                    and fallback_model is not None
                    and _is_exhausted(status_code, resp_text)
                ):
                    fallback_triggered = True
                    active_model = fallback_model
                    break
                if attempt < self.max_attempts:
                    retries_used += 1
                    await asyncio.sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
                    continue
                if active_model == primary_model and fallback_model is not None:
                    fallback_triggered = True
                    active_model = fallback_model
                    break
                GLOBAL_ACCOUNTING.record_failure(call_site=site, retries=retries_used)
                snippet = resp_text[:500] if resp_text else str(e)
                if status_code:
                    raise LLMProviderError(f"LiteLLM error {status_code}: {snippet}") from e
                raise LLMProviderError(f"LiteLLM request failed: {e}") from e

        if fallback_triggered:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    resp = await client.post(
                        url,
                        json=_build_payload(active_model),
                        headers=self._headers(),
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except Exception as e:
                    if attempt < self.max_attempts:
                        retries_used += 1
                        await asyncio.sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
                        continue
                    GLOBAL_ACCOUNTING.record_failure(call_site=site, retries=retries_used)
                    status_code = getattr(getattr(e, "response", None), "status_code", 0)
                    resp_text = getattr(getattr(e, "response", None), "text", "")
                    snippet = resp_text[:500] if resp_text else str(e)
                    raise ProviderExhaustionError(
                        f"Free model exhausted and paid fallback {active_model} failed after "
                        f"{self.max_attempts} attempts (status {status_code}: {snippet})"
                    ) from e

        if data is None:
            GLOBAL_ACCOUNTING.record_failure(call_site=site, retries=retries_used)
            raise LLMProviderError("No response received from model endpoint")

        try:
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            text = msg.get("content") or choice.get("text") or ""
            usage = data.get("usage") or {}
            p_tokens = int(usage.get("prompt_tokens") or 0)
            c_tokens = int(usage.get("completion_tokens") or 0)
            reported_cost = usage.get("cost")
            cost_usd = compute_cost_usd(
                active_model,
                p_tokens,
                c_tokens,
                float(reported_cost) if reported_cost is not None else None,
            )

            accounting_meta = {
                "call_site": site,
                "model_used": active_model,
                "prompt_tokens": p_tokens,
                "completion_tokens": c_tokens,
                "total_tokens": p_tokens + c_tokens,
                "cost_usd": cost_usd,
                "fallback_used": fallback_triggered,
                "retries": retries_used,
            }

            GLOBAL_ACCOUNTING.record_call(
                call_site=site,
                model=active_model,
                prompt_tokens=p_tokens,
                completion_tokens=c_tokens,
                cost_usd=cost_usd,
                retries=retries_used,
                fallback=fallback_triggered,
            )

            return GenerationResult(
                text=str(text),
                model=active_model,
                finish_reason=choice.get("finish_reason"),
                prompt_tokens=p_tokens,
                completion_tokens=c_tokens,
                metadata=_trim_metadata(data, accounting_meta),
            )
        except Exception as e:
            GLOBAL_ACCOUNTING.record_failure(call_site=site, retries=retries_used)
            raise LLMProviderError(f"LiteLLM response parse failed: {e}") from e

    # -----------------------------------------------------------------------
    # LLMProvider interface: stream
    # -----------------------------------------------------------------------

    async def stream(
        self,
        request: GenerationRequest,
        *,
        call_site: str | None = None,
    ) -> AsyncIterator[str]:
        site = call_site or getattr(request, "call_site", None) or self.default_call_site
        _guard_model_name(request.model)
        req_model = request.model.strip()
        _guard_generation_model(req_model)

        is_gemma = "gemma" in req_model.lower()
        if is_gemma:
            primary_model = DEFAULT_CHAT_FREE_MODEL
            fallback_model = DEFAULT_CHAT_PAID_MODEL if self.allow_paid_fallback else None
        else:
            primary_model = req_model
            fallback_model = None

        client = self._http()
        url = f"{self.base_url}/chat/completions"

        def _build_payload(m: str) -> dict[str, Any]:
            p: dict[str, Any] = {
                "model": m,
                "messages": _to_openai_messages(request.messages),
                "stream": True,
                # Compatibility for OpenAI-compatible gateways. OpenRouter
                # itself now always reports final usage (include_usage is deprecated).
                "stream_options": {"include_usage": True},
            }
            if request.temperature is not None:
                p["temperature"] = request.temperature
            if request.max_tokens is not None:
                p["max_tokens"] = request.max_tokens
            if request.stop:
                p["stop"] = request.stop
            return p

        active_model = primary_model
        fallback_triggered = False
        usage = stream_usage_ctx.get()
        usage = usage if usage is not None else StreamUsage()
        usage.model = active_model
        usage.model_status = "selected"

        def _decode(raw_data: str) -> str | None:
            try:
                obj = json.loads(raw_data)
            except json.JSONDecodeError:
                return None
            if not isinstance(obj, dict):
                return None
            # Usage-only frames must be observed before looking at choices.
            usage.observe(obj)
            if obj.get("error") is not None:
                GLOBAL_ACCOUNTING.record_failure(call_site=site)
                raise LLMProviderError("OpenRouter stream reported an error")
            choices = obj.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                return None
            if choices[0].get("finish_reason") == "error":
                GLOBAL_ACCOUNTING.record_failure(call_site=site)
                raise LLMProviderError("OpenRouter stream reported an error")
            delta = choices[0].get("delta")
            chunk = delta.get("content") if isinstance(delta, dict) else None
            return chunk if isinstance(chunk, str) and chunk else None

        # Attempt connect
        try:
            async with client.stream(
                "POST",
                url,
                json=_build_payload(active_model),
                headers=self._headers(),
            ) as resp:
                if (
                    resp.status_code in (429, 402)
                    and active_model == primary_model
                    and fallback_model is not None
                ):
                    fallback_triggered = True
                    active_model = fallback_model
                else:
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as e:
                        if (
                            active_model == primary_model
                            and fallback_model is not None
                            and _is_exhausted(e.response.status_code, "")
                        ):
                            fallback_triggered = True
                            active_model = fallback_model
                        else:
                            GLOBAL_ACCOUNTING.record_failure(call_site=site)
                            raise LLMProviderError(
                                f"LiteLLM stream error {e.response.status_code}"
                            ) from e

                    if not fallback_triggered:
                        async for line in resp.aiter_lines():
                            if not line.strip() or not line.startswith("data:"):
                                continue
                            raw_data = line[5:].strip()
                            if raw_data == "[DONE]":
                                break
                            chunk = _decode(raw_data)
                            if chunk:
                                yield chunk
        except LLMProviderError:
            raise
        except Exception as e:
            if active_model == primary_model and fallback_model is not None:
                fallback_triggered = True
                active_model = fallback_model
            else:
                GLOBAL_ACCOUNTING.record_failure(call_site=site)
                raise LLMProviderError(f"LiteLLM stream failed: {e}") from e

        if fallback_triggered:
            usage.model = active_model
            usage.model_status = "selected"
            # A failed primary response's usage must not describe its fallback.
            usage.prompt_tokens = usage.completion_tokens = usage.total_tokens = None
            usage.cost_usd = None
            try:
                async with client.stream(
                    "POST",
                    url,
                    json=_build_payload(active_model),
                    headers=self._headers(),
                ) as resp:
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as e:
                        GLOBAL_ACCOUNTING.record_failure(call_site=site)
                        raise ProviderExhaustionError(
                            "Free model stream exhausted and paid fallback stream error "
                            f"{e.response.status_code}"
                        ) from e
                    async for line in resp.aiter_lines():
                        if not line.strip() or not line.startswith("data:"):
                            continue
                        raw_data = line[5:].strip()
                        if raw_data == "[DONE]":
                            break
                        chunk = _decode(raw_data)
                        if chunk:
                            yield chunk
            except LLMProviderError:
                raise
            except Exception as e:
                GLOBAL_ACCOUNTING.record_failure(call_site=site)
                raise LLMProviderError(f"LiteLLM stream failed: {e}") from e

        # Sum reported counts only; chunk boundaries are not token boundaries.
        # Unknown usage/cost stays explicit instead of becoming a fabricated zero.
        GLOBAL_ACCOUNTING.record_call(
            call_site=site,
            model=active_model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
            fallback=fallback_triggered,
        )

    # -----------------------------------------------------------------------
    # EmbeddingProvider interface: embed_documents & embed_query (async)
    # -----------------------------------------------------------------------

    async def embed_documents(
        self,
        texts: Sequence[str],
        *,
        call_site: str | None = None,
    ) -> list[list[float]]:
        site = call_site or self.default_call_site
        batch = list(texts)
        if not batch:
            return []

        client = self._http()
        url = f"{self.base_url}/embeddings"
        payload = {
            "model": DEFAULT_EMBEDDING_MODEL,
            "input": batch,
        }
        retries_used = 0
        data: dict[str, Any] | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = await client.post(url, json=payload, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
                vectors = _check_embedding_response(data, len(batch), DEFAULT_EMBEDDING_MODEL)
                break
            except Exception as e:
                if attempt < self.max_attempts:
                    retries_used += 1
                    await asyncio.sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
                    continue
                GLOBAL_ACCOUNTING.record_failure(call_site=site, retries=retries_used)
                raise EmbedderError(
                    f"{DEFAULT_EMBEDDING_MODEL}: embedding failed after {self.max_attempts} "
                    f"attempts (last error: {type(e).__name__}: {e})"
                ) from e

        for vec in vectors:
            if len(vec) != DEFAULT_EMBEDDING_DIMENSION:
                GLOBAL_ACCOUNTING.record_failure(call_site=site, retries=retries_used)
                raise EmbedderError(
                    f"{DEFAULT_EMBEDDING_MODEL}: expected {DEFAULT_EMBEDDING_DIMENSION}-d, "
                    f"got {len(vec)}-d"
                )

        usage = data.get("usage") or {} if isinstance(data, dict) else {}
        p_tokens = int(usage.get("prompt_tokens") or 0)
        reported_cost = usage.get("cost")
        cost_usd = compute_cost_usd(
            DEFAULT_EMBEDDING_MODEL,
            p_tokens,
            0,
            float(reported_cost) if reported_cost is not None else None,
        )

        GLOBAL_ACCOUNTING.record_call(
            call_site=site,
            model=DEFAULT_EMBEDDING_MODEL,
            prompt_tokens=p_tokens,
            completion_tokens=0,
            cost_usd=cost_usd,
            retries=retries_used,
            vectors=len(vectors),
        )

        return vectors

    async def embed_query(
        self,
        text: str,
        *,
        call_site: str | None = None,
    ) -> list[float]:
        vectors = await self.embed_documents([text], call_site=call_site)
        return vectors[0]

    # -----------------------------------------------------------------------
    # Sync helpers: embed & chat (for evals & scripts)
    # -----------------------------------------------------------------------

    def embed(
        self,
        texts: Sequence[str],
        *,
        call_site: str | None = None,
    ) -> list[list[float]]:
        """Synchronous embed method matching evals.embedder_clients.Embedder."""
        site = call_site or self.default_call_site
        batch = list(texts)
        if not batch:
            return []

        client = self._http_sync()
        url = f"{self.base_url}/embeddings"
        payload = {
            "model": DEFAULT_EMBEDDING_MODEL,
            "input": batch,
        }
        retries_used = 0
        data: dict[str, Any] | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = client.post(url, json=payload, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
                vectors = _check_embedding_response(data, len(batch), DEFAULT_EMBEDDING_MODEL)
                break
            except Exception as e:
                if attempt < self.max_attempts:
                    retries_used += 1
                    time.sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
                    continue
                GLOBAL_ACCOUNTING.record_failure(call_site=site, retries=retries_used)
                raise EmbedderError(
                    f"{DEFAULT_EMBEDDING_MODEL}: embedding failed after {self.max_attempts} "
                    f"attempts (last error: {type(e).__name__}: {e})"
                ) from e

        for vec in vectors:
            if len(vec) != DEFAULT_EMBEDDING_DIMENSION:
                GLOBAL_ACCOUNTING.record_failure(call_site=site, retries=retries_used)
                raise EmbedderError(
                    f"{DEFAULT_EMBEDDING_MODEL}: expected {DEFAULT_EMBEDDING_DIMENSION}-d, "
                    f"got {len(vec)}-d"
                )

        usage = data.get("usage") or {} if isinstance(data, dict) else {}
        p_tokens = int(usage.get("prompt_tokens") or 0)
        reported_cost = usage.get("cost")
        cost_usd = compute_cost_usd(
            DEFAULT_EMBEDDING_MODEL,
            p_tokens,
            0,
            float(reported_cost) if reported_cost is not None else None,
        )

        GLOBAL_ACCOUNTING.record_call(
            call_site=site,
            model=DEFAULT_EMBEDDING_MODEL,
            prompt_tokens=p_tokens,
            completion_tokens=0,
            cost_usd=cost_usd,
            retries=retries_used,
            vectors=len(vectors),
        )

        return vectors

    def chat(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        model: str | None = None,
        call_site: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Synchronous chat helper for probe/eval scripts, replacing ad-hoc urllib."""
        if isinstance(prompt, str):
            messages = [{"role": "user", "content": prompt}]
        else:
            messages = prompt

        req_model = (model or DEFAULT_CHAT_FREE_MODEL).strip()
        _guard_model_name(req_model)
        _guard_generation_model(req_model)

        site = call_site or self.default_call_site
        is_gemma = "gemma" in req_model.lower()
        primary_model = DEFAULT_CHAT_FREE_MODEL if is_gemma else req_model
        fallback_model = (
            DEFAULT_CHAT_PAID_MODEL if (is_gemma and self.allow_paid_fallback) else None
        )

        client = self._http_sync()
        url = f"{self.base_url}/chat/completions"

        def _build_p(m: str) -> dict[str, Any]:
            p: dict[str, Any] = {
                "model": m,
                "messages": messages,
                "temperature": temperature,
            }
            if max_tokens is not None:
                p["max_tokens"] = max_tokens
            if tools:
                p["tools"] = tools
                p["tool_choice"] = "auto"
            return p

        retries_used = 0
        fallback_triggered = False
        active_model = primary_model
        data: dict[str, Any] | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = client.post(url, json=_build_p(active_model), headers=self._headers())
                if (
                    resp.status_code in (429, 402)
                    and active_model == primary_model
                    and fallback_model is not None
                ):
                    fallback_triggered = True
                    active_model = fallback_model
                    break
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                status_code = getattr(getattr(e, "response", None), "status_code", 0)
                resp_text = getattr(getattr(e, "response", None), "text", "")
                if (
                    active_model == primary_model
                    and fallback_model is not None
                    and _is_exhausted(status_code, resp_text)
                ):
                    fallback_triggered = True
                    active_model = fallback_model
                    break
                if attempt < self.max_attempts:
                    retries_used += 1
                    time.sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
                    continue
                if active_model == primary_model and fallback_model is not None:
                    fallback_triggered = True
                    active_model = fallback_model
                    break
                GLOBAL_ACCOUNTING.record_failure(call_site=site, retries=retries_used)
                raise LLMProviderError(f"OpenRouter chat failed: {e}") from e

        if fallback_triggered:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    resp = client.post(url, json=_build_p(active_model), headers=self._headers())
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except Exception as e:
                    if attempt < self.max_attempts:
                        retries_used += 1
                        time.sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
                        continue
                    GLOBAL_ACCOUNTING.record_failure(call_site=site, retries=retries_used)
                    raise ProviderExhaustionError(
                        f"Free model exhausted and paid fallback {active_model} failed after "
                        f"{self.max_attempts} attempts: {e}"
                    ) from e

        if data is None:
            GLOBAL_ACCOUNTING.record_failure(call_site=site, retries=retries_used)
            raise LLMProviderError("No response received from model endpoint")

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        p_tokens = int(usage.get("prompt_tokens") or 0)
        c_tokens = int(usage.get("completion_tokens") or 0)
        reported_cost = usage.get("cost")
        cost_usd = compute_cost_usd(
            active_model,
            p_tokens,
            c_tokens,
            float(reported_cost) if reported_cost is not None else None,
        )

        GLOBAL_ACCOUNTING.record_call(
            call_site=site,
            model=active_model,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            cost_usd=cost_usd,
            retries=retries_used,
            fallback=fallback_triggered,
        )

        return {
            "message": msg,
            "text": msg.get("content") or choice.get("text") or "",
            "model_used": active_model,
            "prompt_tokens": p_tokens,
            "completion_tokens": c_tokens,
            "cost_usd": cost_usd,
            "fallback_used": fallback_triggered,
            "retries": retries_used,
            "raw": data,
        }


def create_openrouter_provider(**kwargs: Any) -> OpenRouterProvider:
    """Factory creating an OpenRouterProvider instance."""
    return OpenRouterProvider(**kwargs)
