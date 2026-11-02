"""Vision gateway: the OpenRouter key is sent only to an openrouter.ai host.

The vision step resolves its base URL and key the way OpenRouterProvider does
(the embed path), so an operator's LITELLM_URL never receives the OpenRouter key.
Plain sync; asyncio.run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

import httpx
import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion.adapters import pdfium_render
from jobs import ingest_pipeline as ip
from models.adapters import openrouter_provider

pytestmark = pytest.mark.unit

WS = uuid.uuid4()
OR_KEY = "sk-or-test-key"


class _Gateway:
    """Real transport and adapter; only page rendering and the HTTP client are replaced."""

    def __init__(self, monkeypatch) -> None:
        self.requests: list[httpx.Request] = []
        real_client = httpx.AsyncClient
        seen = self.requests

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "a chart"}}]})

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
        )
        monkeypatch.setattr(pdfium_render, "get_pdf_page_count", lambda _c: 1)
        monkeypatch.setattr(
            pdfium_render, "render_pdf_page", lambda _c, _p, dpi=150: (b"png", 612.0, 792.0, 1, 1)
        )
        monkeypatch.setattr(ip, "VISION_MAX_PAGES", 5)
        monkeypatch.setattr(ip, "VISION_IMAGE_BUDGET_BYTES", 10_000)
        monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
        monkeypatch.delenv("LITELLM_API_KEY", raising=False)
        monkeypatch.delenv("LITELLM_URL", raising=False)

    def run(self):
        return asyncio.run(
            ip._vision_elements_for_pdf(
                b"%PDF", document=None, workspace_id=WS, sensitive=False, pages=[1]
            )
        )


def _events(caplog, name):
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def test_default_gateway_gets_the_openrouter_key(monkeypatch) -> None:
    gw = _Gateway(monkeypatch)
    assert len(gw.run()) == 1
    (req,) = gw.requests
    assert req.url.host == "openrouter.ai"
    assert req.url.path == "/api/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {OR_KEY}"


def test_litellm_url_on_another_host_never_receives_the_openrouter_key(monkeypatch) -> None:
    gw = _Gateway(monkeypatch)
    monkeypatch.setenv("LITELLM_URL", "https://evil.example/v1")
    gw.run()
    assert gw.requests, "vision made no call"
    # OPENROUTER_API_KEY set means OpenRouterProvider resolves to openrouter.ai.
    assert {r.url.host for r in gw.requests} == {"openrouter.ai"}


def test_missing_openrouter_key_skips_and_sends_nothing(monkeypatch, caplog) -> None:
    gw = _Gateway(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setenv("LITELLM_URL", "https://evil.example/v1")
    monkeypatch.setenv("LITELLM_API_KEY", "lk")
    with caplog.at_level(logging.WARNING, logger=ip.logger.name):
        assert gw.run() == []
    assert gw.requests == []
    assert len(_events(caplog, "vision_no_api_key")) == 1


@pytest.mark.parametrize(
    ("base_url", "own_key", "sends"),
    [
        ("https://gateway.example/v1", "lk", True),  # other host: its own key
        ("https://gateway.example/v1", None, False),  # other host, no key: skip
        ("https://openrouter.ai.evil.example/v1", OR_KEY, False),  # lookalike host
    ],
)
def test_non_openrouter_host_gets_its_own_key_or_skips(
    monkeypatch, caplog, base_url, own_key, sends
) -> None:
    gw = _Gateway(monkeypatch)

    class _Provider:
        def __init__(self) -> None:
            self.base_url = base_url
            self.api_key = own_key

    monkeypatch.setattr(openrouter_provider, "OpenRouterProvider", _Provider)
    with caplog.at_level(logging.WARNING, logger=ip.logger.name):
        gw.run()
    assert not any(OR_KEY in r.headers.get("authorization", "") for r in gw.requests)
    if sends:
        (req,) = gw.requests
        assert req.url.host == "gateway.example"
        assert req.headers["authorization"] == "Bearer lk"
    else:
        assert gw.requests == []
        assert len(_events(caplog, "vision_no_api_key")) == 1
