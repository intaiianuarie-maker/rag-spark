"""Cost/plan controls on the opt-in vision step (plain sync; asyncio.run).

Covers: render only text-layer-lost pages, language pinned before vision
elements join, free->paid fallback only on exhaustion-type errors, and no
vision spend on a job retry.
"""

from __future__ import annotations

import asyncio
import os
import types
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion.adapters import pdfium_render
from ingestion.interfaces import DocumentElement, ElementType, ParsedDocument
from ingestion.metadata import extract_metadata
from jobs import ingest_pipeline as ip
from models.interfaces import MessageRole
from models.vision import (
    MultimodalMessage,
    OpenRouterVisionAdapter,
    TextContentPart,
    VisionGenerationRequest,
    VisionPolicyError,
)

pytestmark = pytest.mark.unit

WS = uuid.uuid4()


# --------------------------------------------------------------------------- A
class _Pipe:
    def __init__(self, monkeypatch, *, total=40):
        self.renders: list[int] = []
        self.http: list[dict] = []
        env = self

        def render(_c, page, dpi=150):
            env.renders.append(page)
            return b"png".ljust(50, b"x"), 612.0, 792.0, 1, 1

        class Transport:
            def __init__(self, *a):
                pass

            async def post_completion(self, payload):
                env.http.append(payload)
                return {"choices": [{"message": {"content": "desc"}}]}

        monkeypatch.setattr(pdfium_render, "get_pdf_page_count", lambda _c: total)
        monkeypatch.setattr(pdfium_render, "render_pdf_page", render)
        monkeypatch.setattr(ip, "_OpenRouterVisionTransport", Transport)
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    def run(self, pages, sensitive=False):
        return asyncio.run(
            ip._vision_elements_for_pdf(
                b"%PDF", document=None, workspace_id=WS, sensitive=sensitive, pages=pages
            )
        )


def test_no_lost_pages_means_no_render_and_no_http(monkeypatch, caplog) -> None:
    p = _Pipe(monkeypatch)
    with caplog.at_level("INFO", logger=ip.logger.name):
        assert p.run([]) == []
    assert p.renders == [] and p.http == []
    logged = [r for r in caplog.records if getattr(r, "event", None) == "vision_no_lost_pages"]
    assert len(logged) == 1


def test_many_lost_pages_are_all_sent_sorted_and_deduped(monkeypatch) -> None:
    monkeypatch.setattr(ip, "VISION_MAX_PAGES", None)  # production: unlimited
    p = _Pipe(monkeypatch)
    elements = p.run([30, 3, 12, 7, 7, 20, 1, 25])
    assert p.renders == [1, 3, 7, 12, 20, 25, 30]  # sorted and deduped, none dropped
    assert [e.page for e in elements] == [1, 3, 7, 12, 20, 25, 30]


def test_sensitive_workspace_with_lost_pages_still_makes_no_call(monkeypatch) -> None:
    p = _Pipe(monkeypatch)
    with pytest.raises(VisionPolicyError):
        p.run([1, 2], sensitive=True)
    assert p.http == []


def test_apply_step_passes_only_lost_pages_and_none_means_no_call(monkeypatch) -> None:
    seen: list = []

    async def fake(content, *, document, workspace_id, sensitive, pages):
        seen.append(list(pages))
        return []

    monkeypatch.setattr(ip, "_vision_elements_for_pdf", fake)
    monkeypatch.setattr(ip, "_vision_workspace_sensitive", lambda _w: False)
    job = types.SimpleNamespace(id=uuid.uuid4(), workspace_id=WS, attempts=1)
    parsed = ParsedDocument(elements=[])
    asyncio.run(ip._apply_vision_step(parsed, b"%PDF", document=None, job=job, lost_pages=(4, 2)))
    assert seen == [[4, 2]]  # sorting/capping is the render function's job


# --------------------------------------------------------------------------- 7
def _el(text, **meta):
    return DocumentElement(type=ElementType.TEXT, text=text, metadata=meta)


_RO = (
    "Contract de furnizare intre parti. Vanzatorul livreaza marfa in zece zile. "
    "Plata se face la termen."
)
_EN = [
    f"[Model-generated page description, page {i}] This page shows a chart of the quarterly "
    "revenue with a table of the figures and the caption for the report."
    for i in range(1, 6)
]


def _ro_doc():
    return ParsedDocument(elements=[_el(_RO)])


def _step(parsed, monkeypatch, *, attempts=1):
    async def fake(content, *, document, workspace_id, sensitive, pages):
        return [_el(t, extraction="vision") for t in _EN]

    monkeypatch.setattr(ip, "_vision_elements_for_pdf", fake)
    monkeypatch.setattr(ip, "_vision_workspace_sensitive", lambda _w: False)
    job = types.SimpleNamespace(id=uuid.uuid4(), workspace_id=WS, attempts=attempts)
    asyncio.run(ip._apply_vision_step(parsed, b"%PDF", document=None, job=job, lost_pages=(1,)))


def test_romanian_without_diacritics_stays_ro_after_vision(monkeypatch) -> None:
    before = extract_metadata(_ro_doc()).language
    assert before == "ro"
    unpinned = _ro_doc()
    unpinned.elements.extend(_el(t, extraction="vision") for t in _EN)
    assert extract_metadata(unpinned).language == "en"  # control: the flip is real
    parsed = _ro_doc()
    _step(parsed, monkeypatch)
    assert len(parsed.elements) == 6  # vision elements were added
    assert extract_metadata(parsed).language == "ro"


def test_declared_language_is_not_overridden_by_pin(monkeypatch) -> None:
    parsed = ParsedDocument(elements=[_el(_RO)], language="pt")
    _step(parsed, monkeypatch)
    assert parsed.language == "pt"


# --------------------------------------------------------------------------- 9a
def _req(sensitive=False):
    return VisionGenerationRequest(
        messages=[MultimodalMessage(role=MessageRole.USER, parts=[TextContentPart(text="x")])],
        workspace_id="w",
        is_sensitive=sensitive,
        allow_paid_fallback=True,
    )


def _http_error(status):
    request = httpx.Request("POST", "https://x/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("e", request=request, response=response)


def _adapter(first_error):
    transport = AsyncMock()
    transport.post_completion.side_effect = [
        first_error,
        {"choices": [{"message": {"content": "paid"}}]},
    ]
    return OpenRouterVisionAdapter(transport=transport), transport


@pytest.mark.parametrize(
    "error",
    [
        _http_error(402),
        _http_error(429),
        _http_error(500),
        _http_error(503),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("down"),
        TimeoutError("t"),
        ConnectionError("c"),
    ],
)
def test_exhaustion_errors_fall_back_to_paid(error) -> None:
    adapter, transport = _adapter(error)
    res = asyncio.run(adapter.generate_vision(_req()))
    assert res.is_paid is True and res.text == "paid"
    assert transport.post_completion.await_count == 2


@pytest.mark.parametrize(
    "error", [_http_error(400), _http_error(401), _http_error(404), ValueError("content null")]
)
def test_other_free_route_errors_do_not_call_paid(error) -> None:
    adapter, transport = _adapter(error)
    with pytest.raises(type(error)):
        asyncio.run(adapter.generate_vision(_req()))
    assert transport.post_completion.await_count == 1


def test_sensitive_still_raises_before_any_http() -> None:
    transport = AsyncMock()
    adapter = OpenRouterVisionAdapter(transport=transport)
    req = _req(sensitive=True).model_copy(update={"allow_paid_fallback": False})
    with pytest.raises(VisionPolicyError):
        asyncio.run(adapter.generate_vision(req))
    transport.post_completion.assert_not_called()


# --------------------------------------------------------------------------- 9b
def test_retry_attempt_skips_vision_entirely(monkeypatch, caplog) -> None:
    called: list = []

    async def fake(*a, **k):
        called.append(1)
        return [_el("x")]

    monkeypatch.setattr(ip, "_vision_elements_for_pdf", fake)
    monkeypatch.setattr(ip, "_vision_workspace_sensitive", lambda _w: False)
    job = types.SimpleNamespace(id=uuid.uuid4(), workspace_id=WS, attempts=2)
    parsed = ParsedDocument(elements=[_el(_RO)])
    with caplog.at_level("INFO", logger=ip.logger.name):
        asyncio.run(ip._apply_vision_step(parsed, b"%PDF", document=None, job=job, lost_pages=(1,)))
    assert called == [] and len(parsed.elements) == 1
    skipped = [r for r in caplog.records if getattr(r, "event", None) == "vision_skipped_retry"]
    assert len(skipped) == 1
