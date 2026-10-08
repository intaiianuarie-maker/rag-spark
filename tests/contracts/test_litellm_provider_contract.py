"""LiteLLM provider contract (TASK-M4-001).

Runs the shared `check_llm_provider_contract` against `LiteLLMProvider`
pointed at a **fake httpx transport** that mimics LiteLLM's OpenAI-compatible
`/chat/completions` (JSON + SSE). No gateway, no API keys.

Also pins adapter-specific promises: generate parse/passthrough/usage,
SSE chunks + malformed-line skip + `[DONE]`, empty-model pre-network error,
typed errors on 503 / non-JSON / unreachable, injected-client lifecycle
(not closed), and the **auth-header pin** — an injected client still sends
`Authorization` (Major #2 trap).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from models.adapters.litellm_provider import LiteLLMProvider, LLMProviderError
from models.contract_tests import check_llm_provider_contract
from models.interfaces import ChatMessage, GenerationRequest, MessageRole

pytestmark = [pytest.mark.unit, pytest.mark.contract]

MODEL = "contract-model"

SSE_BODY = (
    'data: {"choices": [{"delta": {"content": "hello "}}]}\n'
    "data: not-json{\n"
    'data: {"choices": [{"delta": {"content": "world"}}]}\n'
    "data: [DONE]\n"
)


def _request(model: str = MODEL) -> GenerationRequest:
    return GenerationRequest(
        model=model,
        messages=[ChatMessage(role=MessageRole.USER, content="ping")],
        temperature=0.0,
    )


def _gateway(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    assert "/chat/completions" in str(request.url)
    body = json.loads(request.content.decode())
    assert body["model"] == MODEL
    assert body["messages"] == [{"role": "user", "content": "ping"}]
    if body.get("stream"):
        return httpx.Response(
            200,
            content=SSE_BODY.encode(),
            headers={"content-type": "text/event-stream"},
        )
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"content": "hello world"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            "model": body["model"],
        },
    )


def _provider(**kwargs: Any) -> LiteLLMProvider:
    return LiteLLMProvider(
        base_url="http://fake-gateway",
        client=httpx.AsyncClient(transport=_gateway(_ok_handler)),
        **kwargs,
    )


# --- shared contract ----------------------------------------------------------


async def test_adapter_satisfies_llm_contract() -> None:
    provider = _provider()
    try:
        await check_llm_provider_contract(provider)
    finally:
        await provider._client.aclose()  # type: ignore[union-attr]


# --- generate -----------------------------------------------------------------


async def test_generate_parses_text_and_passthrough() -> None:
    provider = _provider()
    try:
        result = await provider.generate(_request())
        assert result.text == "hello world"
        assert result.model == MODEL
        assert result.prompt_tokens == 7
        assert result.completion_tokens == 2
        assert result.finish_reason == "stop"
    finally:
        await provider._client.aclose()  # type: ignore[union-attr]


# --- stream -------------------------------------------------------------------


async def test_stream_chunks_skip_malformed_and_stop_at_done() -> None:
    provider = _provider()
    try:
        chunks = [chunk async for chunk in provider.stream(_request())]
        assert chunks == ["hello ", "world"]
        assert "".join(chunks) == "hello world"
    finally:
        await provider._client.aclose()  # type: ignore[union-attr]


# --- errors -------------------------------------------------------------------


async def test_empty_model_raises_pre_network() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise AssertionError("must not reach the network")

    provider = LiteLLMProvider(
        base_url="http://fake-gateway",
        client=httpx.AsyncClient(transport=_gateway(handler)),
    )
    with pytest.raises(LLMProviderError):
        await provider.generate(GenerationRequest(model="", messages=[]))
    assert seen == []


async def test_http_503_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "busy"}})

    provider = LiteLLMProvider(
        base_url="http://fake-gateway",
        client=httpx.AsyncClient(transport=_gateway(handler)),
    )
    try:
        with pytest.raises(LLMProviderError, match="503"):
            await provider.generate(_request())
    finally:
        await provider._client.aclose()  # type: ignore[union-attr]


async def test_non_json_body_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    provider = LiteLLMProvider(
        base_url="http://fake-gateway",
        client=httpx.AsyncClient(transport=_gateway(handler)),
    )
    try:
        with pytest.raises(LLMProviderError):
            await provider.generate(_request())
    finally:
        await provider._client.aclose()  # type: ignore[union-attr]


async def test_unreachable_server_is_typed() -> None:
    adapter = LiteLLMProvider(base_url="http://127.0.0.1:1", timeout=1.0)
    with pytest.raises(LLMProviderError):
        await adapter.generate(_request())


# --- injected-client lifecycle + auth pin -------------------------------------


async def test_injected_client_is_not_closed_by_provider() -> None:
    provider = _provider()
    client = provider._client
    assert client is not None
    await provider.generate(_request())
    assert not client.is_closed
    await client.aclose()


async def test_injected_client_still_sends_auth_header() -> None:
    """Major #2 pin: LITELLM_API_KEY must not vanish on the injected path."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    provider = LiteLLMProvider(
        base_url="http://fake-gateway",
        api_key="sk-secret",
        client=httpx.AsyncClient(transport=_gateway(handler)),
    )
    try:
        await provider.generate(_request())
        assert seen["authorization"] == "Bearer sk-secret"
    finally:
        await provider._client.aclose()  # type: ignore[union-attr]


async def test_stream_sends_auth_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(
            200,
            content=SSE_BODY.encode(),
            headers={"content-type": "text/event-stream"},
        )

    provider = LiteLLMProvider(
        base_url="http://fake-gateway",
        api_key="sk-secret",
        client=httpx.AsyncClient(transport=_gateway(handler)),
    )
    try:
        chunks = [chunk async for chunk in provider.stream(_request())]
        assert chunks == ["hello ", "world"]
        assert seen["authorization"] == "Bearer sk-secret"
    finally:
        await provider._client.aclose()  # type: ignore[union-attr]
