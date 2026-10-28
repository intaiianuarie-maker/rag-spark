"""Tests for T-ARCH-02 Single OpenRouter provider surface (chat+embeddings).

Covers:
1. One surface: OpenRouterProvider satisfies LLMProvider and EmbeddingProvider contracts.
2. Gemma free-first: google/gemma-4-26b-a4b-it:free attempted first, cost is $0.00.
3. Automatic paid fallback on exhaustion: HTTP 429/402/exhaustion markers switch to
   google/gemma-4-26b-a4b-it.
4. Paid Gemini-generation stays LOCKED: raises ProviderPolicyError before network contact.
5. Accounting gaps: 0 — every call path (generate, stream, chat, embed_documents,
   embed_query, embed) meters tokens, spend, failures, and retries.
6. Call-site isolation: stats segregated by call_site and aggregated in total summary.
7. Bounded retries & loud exhaustion: 3 attempts with exponential backoff, raises loudly.
8. Gemini-embedding-2: 3072-d validation, prompt metering, and error handling.
9. Live smoke test (authorized small paid spend): real wire verify of embeddings + generation.
"""

from __future__ import annotations

import json
import os
from typing import Any, cast

import httpx
import pytest
from models.adapters.litellm_provider import LiteLLMProvider
from models.adapters.openrouter_provider import (
    DEFAULT_CHAT_FREE_MODEL,
    DEFAULT_CHAT_PAID_MODEL,
    DEFAULT_EMBEDDING_DIMENSION,
    DEFAULT_EMBEDDING_MODEL,
    GLOBAL_ACCOUNTING,
    EmbedderError,
    LLMProviderError,
    OpenRouterProvider,
    ProviderPolicyError,
)
from models.interfaces import ChatMessage, GenerationRequest, MessageRole
from retrieval.interfaces import EmbeddingProvider

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _reset_accounting():
    GLOBAL_ACCOUNTING.reset()
    yield
    GLOBAL_ACCOUNTING.reset()


def _make_request(
    model: str = DEFAULT_CHAT_FREE_MODEL, content: str = "Hello", **kwargs: Any
) -> GenerationRequest:
    return GenerationRequest(
        model=model,
        messages=[ChatMessage(role=MessageRole.USER, content=content)],
        temperature=0.0,
        **kwargs,
    )


def _chat_ok_body(
    content: str = "pong",
    model: str = DEFAULT_CHAT_FREE_MODEL,
    p_tokens: int = 10,
    c_tokens: int = 5,
    cost: float | None = None,
) -> dict[str, Any]:
    usage: dict[str, Any] = {"prompt_tokens": p_tokens, "completion_tokens": c_tokens}
    if cost is not None:
        usage["cost"] = cost
    return {
        "id": "chatcmpl-test",
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": usage,
        "model": model,
    }


def _embed_ok_body(
    n: int = 1, dim: int = 3072, p_tokens: int = 8, cost: float = 0.0000012
) -> dict[str, Any]:
    return {
        "data": [{"embedding": [0.05] * dim} for _ in range(n)],
        "usage": {"prompt_tokens": p_tokens, "cost": cost},
        "model": DEFAULT_EMBEDDING_MODEL,
    }


# ---------------------------------------------------------------------------
# 1. Surface contract & inheritance
# ---------------------------------------------------------------------------


def test_surface_satisfies_interfaces() -> None:
    provider = OpenRouterProvider(base_url="http://fake-gateway", api_key="sk-test")
    assert isinstance(provider, EmbeddingProvider)
    assert provider.model_name == DEFAULT_EMBEDDING_MODEL
    assert provider.dimension == DEFAULT_EMBEDDING_DIMENSION

    # LiteLLMProvider is an instance of OpenRouterProvider
    lite = LiteLLMProvider(base_url="http://fake-gateway", api_key="sk-test")
    assert isinstance(lite, OpenRouterProvider)
    assert lite.provider_name == "litellm"


# ---------------------------------------------------------------------------
# 2. Gemma free-first execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemma_free_first_success() -> None:
    seen_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen_models.append(body["model"])
        return httpx.Response(200, json=_chat_ok_body("free response", body["model"], 10, 5))

    transport = httpx.MockTransport(handler)
    provider = OpenRouterProvider(
        base_url="http://fake-gateway",
        api_key="sk-test",
        client=httpx.AsyncClient(transport=transport),
        default_call_site="test_free",
    )

    req = _make_request("google/gemma-4-26b-a4b-it")  # requested gemma without :free
    result = await provider.generate(req)

    # Prove free model was targeted first
    assert seen_models == [DEFAULT_CHAT_FREE_MODEL]
    accounting = cast(dict[str, Any], result.metadata["accounting"])
    assert accounting["cost_usd"] == 0.0
    assert accounting["fallback_used"] is False

    stats = GLOBAL_ACCOUNTING.get_stats("test_free")
    assert stats.n_calls == 1
    assert stats.n_free_calls == 1
    assert stats.n_paid_calls == 0
    assert stats.n_fallbacks == 0
    assert stats.cost_usd == 0.0
    assert stats.prompt_tokens == 10
    assert stats.completion_tokens == 5


# ---------------------------------------------------------------------------
# 3. Automatic paid fallback on pool exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_automatic_paid_fallback_on_429() -> None:
    seen_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        model = body["model"]
        seen_models.append(model)
        if model == DEFAULT_CHAT_FREE_MODEL:
            # Free pool exhausted
            return httpx.Response(
                429,
                json={
                    "error": {
                        "message": "google/gemma-4-26b-a4b-it:free is temporarily rate-limited"
                    }
                },
            )
        # Paid fallback succeeds
        return httpx.Response(200, json=_chat_ok_body("paid response", model, 20, 10))

    transport = httpx.MockTransport(handler)
    provider = OpenRouterProvider(
        base_url="http://fake-gateway",
        api_key="sk-test",
        client=httpx.AsyncClient(transport=transport),
        default_call_site="test_fallback",
    )

    req = _make_request("gemma-4-26b-a4b-it")
    result = await provider.generate(req)

    # Free attempted, then paid fallback
    assert seen_models == [DEFAULT_CHAT_FREE_MODEL, DEFAULT_CHAT_PAID_MODEL]
    assert result.text == "paid response"
    assert result.model == DEFAULT_CHAT_PAID_MODEL
    accounting = cast(dict[str, Any], result.metadata["accounting"])
    assert accounting["fallback_used"] is True
    assert accounting["cost_usd"] > 0.0

    stats = GLOBAL_ACCOUNTING.get_stats("test_fallback")
    assert stats.n_calls == 1
    assert stats.n_fallbacks == 1
    assert stats.n_paid_calls == 1
    assert stats.n_free_calls == 0
    assert stats.cost_usd > 0.0


# ---------------------------------------------------------------------------
# 4. Paid Gemini-generation LOCKED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paid_gemini_generation_is_locked() -> None:
    network_hit = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal network_hit
        network_hit = True
        return httpx.Response(200, json=_chat_ok_body())

    transport = httpx.MockTransport(handler)
    provider = OpenRouterProvider(
        base_url="http://fake-gateway",
        api_key="sk-test",
        client=httpx.AsyncClient(transport=transport),
    )

    locked_models = [
        "google/gemini-2.0-flash",
        "google/gemini-1.5-pro",
        "gemini-2.5-pro",
        "google/gemini-embedding-2",  # embedding model cannot be used for text generation
    ]

    for model in locked_models:
        with pytest.raises(ProviderPolicyError) as exc_info:
            await provider.generate(_make_request(model))
        assert "locked by policy" in str(exc_info.value) or "Embedding model" in str(exc_info.value)

    assert not network_hit, "Paid Gemini requests must never touch the network"


# ---------------------------------------------------------------------------
# 5. Accounting gaps: 0 across all call paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accounting_on_embed_documents() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/embeddings" in str(request.url)
        body = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json=_embed_ok_body(len(body["input"]), 3072, p_tokens=15, cost=0.00000225),
        )

    transport = httpx.MockTransport(handler)
    provider = OpenRouterProvider(
        base_url="http://fake-gateway",
        api_key="sk-test",
        client=httpx.AsyncClient(transport=transport),
        default_call_site="embed_site",
    )

    vecs = await provider.embed_documents(["doc1", "doc2"])
    assert len(vecs) == 2
    assert len(vecs[0]) == 3072

    stats = GLOBAL_ACCOUNTING.get_stats("embed_site")
    assert stats.n_calls == 1
    assert stats.n_vectors == 2
    assert stats.prompt_tokens == 15
    assert stats.cost_usd == pytest.approx(0.00000225)
    assert stats.n_failed_calls == 0


@pytest.mark.asyncio
async def test_accounting_on_embed_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_embed_ok_body(1, 3072, p_tokens=6, cost=0.0000009))

    transport = httpx.MockTransport(handler)
    provider = OpenRouterProvider(
        base_url="http://fake-gateway",
        api_key="sk-test",
        client=httpx.AsyncClient(transport=transport),
        default_call_site="query_site",
    )

    vec = await provider.embed_query("search text")
    assert len(vec) == 3072

    stats = GLOBAL_ACCOUNTING.get_stats("query_site")
    assert stats.n_calls == 1
    assert stats.n_vectors == 1
    assert stats.prompt_tokens == 6
    assert stats.cost_usd == pytest.approx(0.0000009)


def test_sync_embed_and_chat_accounting() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/embeddings" in str(request.url):
            return httpx.Response(200, json=_embed_ok_body(1, 3072, p_tokens=5, cost=0.00000075))
        return httpx.Response(200, json=_chat_ok_body("sync pong", DEFAULT_CHAT_FREE_MODEL, 12, 4))

    transport = httpx.MockTransport(handler)
    provider = OpenRouterProvider(
        base_url="http://fake-gateway",
        api_key="sk-test",
        sync_client=httpx.Client(transport=transport),
        default_call_site="sync_site",
    )

    # Sync embed
    vecs = provider.embed(["sample"])
    assert len(vecs) == 1
    assert len(vecs[0]) == 3072

    # Sync chat
    res = provider.chat("ping")
    assert res["text"] == "sync pong"

    stats = GLOBAL_ACCOUNTING.get_stats("sync_site")
    assert stats.n_calls == 2
    assert stats.n_vectors == 1
    assert stats.prompt_tokens == 17
    assert stats.completion_tokens == 4


# ---------------------------------------------------------------------------
# 6. Call-site segregation & global summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_site_segregation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_ok_body("ok", DEFAULT_CHAT_FREE_MODEL, 10, 5))

    transport = httpx.MockTransport(handler)
    provider = OpenRouterProvider(
        base_url="http://fake-gateway",
        api_key="sk-test",
        client=httpx.AsyncClient(transport=transport),
    )

    await provider.generate(_make_request(), call_site="answer_service")
    await provider.generate(_make_request(), call_site="eval_probe")

    summary = GLOBAL_ACCOUNTING.summary()
    assert summary["total"]["calls"] == 2
    assert summary["sites"]["answer_service"]["calls"] == 1
    assert summary["sites"]["eval_probe"]["calls"] == 1


# ---------------------------------------------------------------------------
# 7. Bounded retries and loud exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bounded_retries_and_loud_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(_: float) -> None:
        pass

    monkeypatch.setattr("asyncio.sleep", _noop)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"error": "server error"})

    transport = httpx.MockTransport(handler)
    provider = OpenRouterProvider(
        base_url="http://fake-gateway",
        api_key="sk-test",
        client=httpx.AsyncClient(transport=transport),
        default_call_site="retry_site",
        max_attempts=3,
        backoff_base_seconds=0.001,
    )

    with pytest.raises(LLMProviderError):
        await provider.generate(_make_request("contract-model"))

    assert calls == 3
    stats = GLOBAL_ACCOUNTING.get_stats("retry_site")
    assert stats.n_calls == 1
    assert stats.n_failed_calls == 1
    assert stats.n_retries == 2
    assert stats.failure_rate == 1.0


# ---------------------------------------------------------------------------
# 8. Embedding dimension check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_wrong_dimension_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_embed_ok_body(1, dim=1024))  # 1024 instead of 3072

    transport = httpx.MockTransport(handler)
    provider = OpenRouterProvider(
        base_url="http://fake-gateway",
        api_key="sk-test",
        client=httpx.AsyncClient(transport=transport),
    )

    with pytest.raises(EmbedderError, match="expected 3072-d"):
        await provider.embed_documents(["test"])


# ---------------------------------------------------------------------------
# 9. Live smoke test (authorized spend, minimal footprint)
# ---------------------------------------------------------------------------


def _live_credential_key() -> str | None:
    """House credential isolation (live_release_stack.py / CREDENTIAL_FILE).

    The environment carries a *path* to a private (0600) JSON file holding the
    key — never the secret itself — so the hygiene gate keeps its teeth.
    """
    key_path = os.environ.get("OPENROUTER_API_KEY_FILE")
    if not key_path or not os.path.isfile(key_path):
        return None
    if os.stat(key_path).st_mode & 0o077:
        return None
    with open(key_path, encoding="utf-8") as handle:
        payload = json.loads(handle.read())
    api_key = payload.get("api_key") if isinstance(payload, dict) else None
    return api_key if isinstance(api_key, str) and api_key else None


@pytest.mark.asyncio
async def test_live_openrouter_wire_verify() -> None:
    api_key = _live_credential_key()
    if not api_key:
        pytest.skip('OPENROUTER_API_KEY_FILE (private JSON {"api_key": ...}) not configured')

    provider = OpenRouterProvider(
        default_call_site="live_smoke",
        timeout=60.0,
    )

    try:
        # 1. Live embedding verify: 1 short text
        vecs = await provider.embed_documents(["antigravity test embedding"])
        assert len(vecs) == 1
        assert len(vecs[0]) == DEFAULT_EMBEDDING_DIMENSION == 3072

        # 2. Live chat generation verify: 1 short prompt
        req = _make_request(
            model=DEFAULT_CHAT_FREE_MODEL,
            content="Reply with exactly the single word: OK",
            max_tokens=5,
        )
        res = await provider.generate(req)
        assert len(res.text.strip()) > 0

        # Check accounting recorded both calls
        stats = GLOBAL_ACCOUNTING.get_stats("live_smoke")
        assert stats.n_calls == 2
        assert stats.n_vectors == 1
        assert stats.prompt_tokens > 0
        assert stats.cost_usd >= 0.0

        print(f"\n[LIVE SMOKE REPORT] calls={stats.n_calls}, vectors={stats.n_vectors}, "
              f"tokens={stats.total_tokens}, spend_usd=${stats.cost_usd:.8f}, "
              f"fallbacks={stats.n_fallbacks}")
    finally:
        await provider.aclose()
        provider.close()
