"""OpenRouter ``embed_multimodal``: real image embedding, loud failures (plain sync)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from models.adapters.openrouter_provider import EmbedderError, OpenRouterProvider
from models.vision import ImageContentPart

pytestmark = pytest.mark.unit


def _provider(handler, **kw) -> OpenRouterProvider:
    return OpenRouterProvider(
        base_url="http://fake-gateway",
        api_key="sk-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_attempts=1,
        backoff_base_seconds=0.0,
        **kw,
    )


def _ok(dim: int = 3072, vec=None):
    emb = vec if vec is not None else [0.5] * dim
    return {"data": [{"embedding": emb}], "usage": {"prompt_tokens": 7}}


PART = ImageContentPart.from_bytes(b"\x89PNG-real", media_type="image/png", page=2)


def test_posts_image_content_part_and_returns_3072d_vector() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=_ok())

    vec = asyncio.run(_provider(handler).embed_multimodal(PART))
    assert len(vec) == 3072
    assert seen["url"].endswith("/embeddings")
    assert seen["body"]["model"] == "google/gemini-embedding-2"
    (item,) = seen["body"]["input"]
    (part,) = item["content"]
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/png;base64,")


def test_api_error_raises() -> None:
    p = _provider(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(EmbedderError):
        asyncio.run(p.embed_multimodal(PART))


def test_wrong_dimension_raises() -> None:
    p = _provider(lambda r: httpx.Response(200, json=_ok(dim=768)))
    with pytest.raises(EmbedderError, match="3072"):
        asyncio.run(p.embed_multimodal(PART))


def test_empty_vector_raises() -> None:
    p = _provider(lambda r: httpx.Response(200, json=_ok(vec=[])))
    with pytest.raises(EmbedderError):
        asyncio.run(p.embed_multimodal(PART))


def test_part_without_image_bytes_never_hits_the_network() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_ok())

    empty = ImageContentPart.from_bytes(b"", media_type="image/png")
    for bad in (empty, ImageContentPart(image_url={})):
        with pytest.raises(EmbedderError, match="no image"):
            asyncio.run(_provider(handler).embed_multimodal(bad))
    assert calls == []
