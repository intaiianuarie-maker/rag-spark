"""embed_multimodal retry policy: transient failures retry, everything else raises at once."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from models.adapters.openrouter_provider import EmbedderError, OpenRouterProvider
from models.vision import ImageContentPart

pytestmark = pytest.mark.unit

MAX = 3
PART = ImageContentPart.from_bytes(b"\x89PNG-real", media_type="image/png", page=2)


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"data": [{"embedding": [0.5] * 3072}]})


def _provider(responder):
    """Provider on a mock gateway; ``responder(n)`` answers the n-th call (1-based)."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responder(len(calls))

    provider = OpenRouterProvider(
        base_url="http://fake-gateway",
        api_key="sk-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_attempts=MAX,
        backoff_base_seconds=0.0,
    )
    return provider, calls


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
def test_client_errors_raise_on_the_first_attempt(status) -> None:
    provider, calls = _provider(
        lambda n: httpx.Response(status, text="B" * 1000, headers={"x-secret": "hdr-value"})
    )
    with pytest.raises(EmbedderError) as exc:
        asyncio.run(provider.embed_multimodal(PART))
    assert len(calls) == 1
    msg = str(exc.value)
    assert "B" * 300 in msg and "B" * 301 not in msg  # body bounded to 300 chars
    assert "hdr-value" not in msg  # no response headers in the error text


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_status_is_retried_up_to_max_attempts(status) -> None:
    provider, calls = _provider(lambda n: httpx.Response(status, text="busy"))
    with pytest.raises(EmbedderError):
        asyncio.run(provider.embed_multimodal(PART))
    assert len(calls) == MAX


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow connect"),
        httpx.ConnectError("down"),
        TimeoutError("t"),
        ConnectionError("c"),
    ],
)
def test_timeouts_and_connection_errors_are_retried_up_to_max_attempts(error) -> None:
    def responder(n: int) -> httpx.Response:
        raise error

    provider, calls = _provider(responder)
    with pytest.raises(EmbedderError):
        asyncio.run(provider.embed_multimodal(PART))
    assert len(calls) == MAX


def test_transient_failure_then_success_returns_the_vector() -> None:
    provider, calls = _provider(lambda n: httpx.Response(503) if n == 1 else _ok())
    vec = asyncio.run(provider.embed_multimodal(PART))
    assert len(vec) == 3072
    assert len(calls) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"data": [{"embedding": [0.5] * 768}]},  # wrong dimension
        {"data": [{"embedding": []}]},  # empty vector
    ],
)
def test_bad_vector_raises_without_retry(body) -> None:
    provider, calls = _provider(lambda n: httpx.Response(200, json=body))
    with pytest.raises(EmbedderError):
        asyncio.run(provider.embed_multimodal(PART))
    assert len(calls) == 1
