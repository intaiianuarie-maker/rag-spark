"""Unit tests for M7-001a OpenAI-compatible provider seams (no network)."""

from __future__ import annotations

import json
import os

import httpx
import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")

from models.interfaces import GenerationRequest, LLMProvider, MessageRole
from models.providers.openai_compat import OpenAICompatProvider
from models.registry import ModelRegistry
from retrieval.interfaces import EmbeddingProvider

pytestmark = pytest.mark.unit


def _provider(base_url: str = "http://stub/v1", model: str = "stub-model") -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name="stub",
        base_url=base_url,
        model=model,
        capabilities={"chat", "reasoning"},
        embedding_dimension=1,
    )


def test_generate_returns_generation_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content)
        assert body["messages"][0] == {"role": "user", "content": "hi"}
        return httpx.Response(
            200,
            json={
                "model": "stub-model",
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    transport = httpx.MockTransport(handler)
    httpx_client = httpx.AsyncClient(transport=transport, base_url="http://stub/v1")
    # patch provider to use client by monkeypatching _client
    p = _provider()

    def _client(client=None):
        return httpx_client if client is None else client

    p._client = _client  # type: ignore[assignment]
    import asyncio

    req = GenerationRequest(model="stub-model", messages=[])  # type: ignore
    from models.interfaces import ChatMessage

    req = GenerationRequest(
        model="stub-model",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    result = asyncio.run(p.generate(req))
    assert result.text == "hello"
    assert result.model == "stub-model"
    assert result.finish_reason == "stop"
    assert result.prompt_tokens == 3
    assert result.completion_tokens == 2


def test_generate_malformed_reply_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    transport = httpx.MockTransport(handler)
    httpx_client = httpx.AsyncClient(transport=transport, base_url="http://stub/v1")
    p = _provider()
    p._client = lambda client=None: httpx_client  # type: ignore[assignment]
    import asyncio

    from models.interfaces import ChatMessage

    req = GenerationRequest(
        model="stub-model",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    with pytest.raises((IndexError, KeyError, RuntimeError)):
        asyncio.run(p.generate(req))


def test_generate_error_status_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(handler)
    httpx_client = httpx.AsyncClient(transport=transport, base_url="http://stub/v1")
    p = _provider()
    p._client = lambda client=None: httpx_client  # type: ignore[assignment]
    import asyncio

    from models.interfaces import ChatMessage

    req = GenerationRequest(
        model="stub-model",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(p.generate(req))


def test_embed_documents_and_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["input"] == ["a", "b"]:
            return httpx.Response(200, json={"data": [{"embedding": [0.1]}, {"embedding": [0.2]}]})
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})

    transport = httpx.MockTransport(handler)

    def make_client(client=None):
        return httpx.AsyncClient(transport=transport, base_url="http://stub/v1")

    p = _provider()
    p._client = make_client  # type: ignore[assignment]
    import asyncio

    async def run():
        docs = await p.embed_documents(["a", "b"])
        q = await p.embed_query("a")
        return docs, q

    docs, q = asyncio.run(run())
    assert docs == [[0.1], [0.2]]
    assert q == [0.1]


def test_stream_yields_chunks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        lines = [
            'data: {"choices":[{"delta":{"content":"hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            "data: [DONE]",
        ]
        return httpx.Response(200, text="\n".join(lines))

    transport = httpx.MockTransport(handler)
    httpx_client = httpx.AsyncClient(transport=transport, base_url="http://stub/v1")
    p = _provider()
    # stream test patches httpx.AsyncClient globally.
    import asyncio

    from models.interfaces import ChatMessage

    req = GenerationRequest(
        model="stub-model",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )

    async def collect():
        # Use httpx.MockTransport by monkeypatching httpx.AsyncClient
        original_client = httpx.AsyncClient
        httpx.AsyncClient = lambda *a, **k: httpx_client  # type: ignore[assignment]
        try:
            out = []
            async for chunk in p.stream(req):
                out.append(chunk)
            return out
        finally:
            httpx.AsyncClient = original_client  # type: ignore[assignment]

    chunks = asyncio.run(collect())
    assert chunks == ["hel", "lo"]


def test_two_providers_registered_with_capabilities() -> None:
    reg = ModelRegistry()
    local = _provider("http://127.0.0.1:9999/v1", "local-stub")
    cloud = _provider("http://cloud.example/v1", "cloud-stub")
    reg.register("local-stub", local, capabilities={"chat"})
    reg.register("cloud-stub", cloud, capabilities={"chat", "reasoning"})
    assert "local-stub" in reg.capability_query("chat")
    assert "cloud-stub" in reg.capability_query("reasoning")
    # both satisfy seams
    assert isinstance(local, LLMProvider)

    assert isinstance(cloud, EmbeddingProvider)


def test_embed_rejects_bad_indices_and_nonfinite() -> None:
    import asyncio

    def make_handler(items):
        def handler(request):
            return httpx.Response(200, json={"data": items})

        return handler

    # negative index
    transport = httpx.MockTransport(make_handler([{"index": -1, "embedding": [0.1]}]))
    client = httpx.AsyncClient(transport=transport, base_url="http://stub/v1")
    p = OpenAICompatProvider(name="s", base_url="http://stub/v1", model="m", embedding_dimension=1)
    p._client = lambda c=None: client  # type: ignore[assignment]
    with pytest.raises(ValueError):
        asyncio.run(p.embed_documents(["a"]))
    # bool index
    transport = httpx.MockTransport(make_handler([{"index": True, "embedding": [0.1]}]))
    client = httpx.AsyncClient(transport=transport, base_url="http://stub/v1")
    p._client = lambda c=None: client  # type: ignore[assignment]
    with pytest.raises(ValueError):
        asyncio.run(p.embed_documents(["a"]))
    # duplicate
    transport = httpx.MockTransport(
        make_handler([{"index": 0, "embedding": [0.1]}, {"index": 0, "embedding": [0.2]}])
    )
    client = httpx.AsyncClient(transport=transport, base_url="http://stub/v1")
    p._client = lambda c=None: client  # type: ignore[assignment]
    with pytest.raises(ValueError):
        asyncio.run(p.embed_documents(["a", "b"]))
    # mixed indexed/unindexed
    transport = httpx.MockTransport(
        make_handler([{"index": 0, "embedding": [0.1]}, {"embedding": [0.2]}])
    )
    client = httpx.AsyncClient(transport=transport, base_url="http://stub/v1")
    p._client = lambda c=None: client  # type: ignore[assignment]
    with pytest.raises(ValueError):
        asyncio.run(p.embed_documents(["a", "b"]))
    # non-finite
    transport = httpx.MockTransport(make_handler([{"embedding": [float("nan")]}]))
    client = httpx.AsyncClient(transport=transport, base_url="http://stub/v1")
    p._client = lambda c=None: client  # type: ignore[assignment]
    with pytest.raises(ValueError):
        asyncio.run(p.embed_documents(["a"]))
    # out-of-order accepted (exact permutation)
    transport = httpx.MockTransport(
        make_handler([{"index": 1, "embedding": [0.2]}, {"index": 0, "embedding": [0.1]}])
    )
    client = httpx.AsyncClient(transport=transport, base_url="http://stub/v1")
    p._client = lambda c=None: client  # type: ignore[assignment]
    res = asyncio.run(p.embed_documents(["a", "b"]))
    assert res == [[0.1], [0.2]]
