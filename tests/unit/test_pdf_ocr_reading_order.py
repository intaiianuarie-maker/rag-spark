"""Mixed native+OCR PDFs keep a page-local, stable reading order."""

from __future__ import annotations

import asyncio
import time
import types

import pytest
from ingestion.adapters import pdf_ocr
from ingestion.interfaces import BBox, DocumentElement, ElementType, PageClassification

pytestmark = pytest.mark.unit


def _el(text, page, y0=None):
    bbox = None if y0 is None else BBox(x0=0, y0=y0, x1=10, y1=y0 + 5)
    return DocumentElement(type=ElementType.TEXT, text=text, page=page, bbox=bbox)


def _texts(els):
    return [e.text for e in els]


def test_native_without_bbox_keeps_source_order_and_ocr_without_bbox_follows():
    native = [_el("n3", 1), _el("n1", 1), _el("n2", 1)]
    out = pdf_ocr._merge_reading_order(native, [_el("o-nobox", 1)])
    assert _texts(out) == ["n3", "n1", "n2", "o-nobox"]


def test_ocr_slots_in_by_y_only_against_native_with_bbox():
    native = [_el("n-top", 1, 10.0), _el("n-low", 1, 200.0)]
    out = pdf_ocr._merge_reading_order(native, [_el("o-mid", 1, 100.0)])
    assert _texts(out) == ["n-top", "o-mid", "n-low"]


def test_pages_are_independent_and_ordered():
    native = [_el("p2-native", 2, 0.0), _el("p1-native", 1)]
    ocr = [_el("p1-ocr", 1, 50.0)]
    assert _texts(pdf_ocr._merge_reading_order(native, ocr)) == ["p1-native", "p1-ocr", "p2-native"]


def test_end_to_end_native_and_scanned_pages(monkeypatch):
    async def fake_parse(*_a, **_k):
        els = [_el("n1-b", 1), _el("n1-a", 1), _el("n3", 3)]
        return types.SimpleNamespace(elements=els), None

    page = types.SimpleNamespace(words=[1], elements=[_el("o2-a", 2, 5.0), _el("o2-b", 2, 9.0)])
    monkeypatch.setattr(
        pdf_ocr,
        "classify_pdf_pages",
        lambda _d: {
            1: PageClassification.NATIVE_TEXT,
            2: PageClassification.OCR_REQUIRED,
            3: PageClassification.NATIVE_TEXT,
        },
    )
    monkeypatch.setattr(pdf_ocr, "parse_with_precedence", fake_parse)
    monkeypatch.setattr(pdf_ocr, "render_pdf_page", lambda *a, **k: (b"", 1, 1, 1, 1))
    monkeypatch.setattr(pdf_ocr, "ocr_page_image", lambda *a, **k: page)
    monkeypatch.setattr(pdf_ocr, "get_tesseract_version", lambda: "t")
    els, report = asyncio.run(pdf_ocr.process_pdf_with_ocr(b"x"))
    assert _texts(els) == ["n1-b", "n1-a", "o2-a", "o2-b", "n3"]
    assert report.ocr_pages == [2]


def test_every_scanned_page_is_ocrd_with_no_page_cap(monkeypatch):
    # The old 50-page cap would have left pages 51-60 in failed_pages.
    seen: list[int] = []

    def ocr(_png, *, page_number, **_k):
        seen.append(page_number)
        return types.SimpleNamespace(words=[1], elements=[])

    monkeypatch.setattr(
        pdf_ocr,
        "classify_pdf_pages",
        lambda _d: {p: PageClassification.OCR_REQUIRED for p in range(1, 61)},
    )
    monkeypatch.setattr(pdf_ocr, "render_pdf_page", lambda *a, **k: (b"", 1, 1, 1, 1))
    monkeypatch.setattr(pdf_ocr, "ocr_page_image", ocr)
    monkeypatch.setattr(pdf_ocr, "get_tesseract_version", lambda: "t")
    _els, report = asyncio.run(pdf_ocr.process_pdf_with_ocr(b"x"))
    assert seen == list(range(1, 61))
    assert report.failed_pages == []


def test_ocr_only_page_keeps_tesseract_column_order():
    # Tesseract emits column by column. Re-sorting OCR lines by y interleaved the columns.
    left = [_el(f"RO{i}", 1, y) for i, y in enumerate((100.0, 150.0, 200.0))]
    right = [_el(f"EN{i}", 1, y) for i, y in enumerate((100.0, 150.0, 200.0))]
    out = pdf_ocr._merge_reading_order([], left + right)
    assert _texts(out) == ["RO0", "RO1", "RO2", "EN0", "EN1", "EN2"]


def test_ocr_lines_are_placed_against_native_only_not_each_other():
    native = [_el("n-top", 1, 10.0), _el("n-low", 1, 200.0)]
    ocr = [_el("RO0", 1, 100.0), _el("RO1", 1, 120.0), _el("EN0", 1, 110.0)]
    out = pdf_ocr._merge_reading_order(native, ocr)
    assert _texts(out) == ["n-top", "RO0", "RO1", "EN0", "n-low"]


def test_ocr_pages_run_off_the_event_loop(monkeypatch):
    # The lease heartbeat is a task on the same loop, so a blocking OCR page must not stall it.
    monkeypatch.setattr(
        pdf_ocr,
        "classify_pdf_pages",
        lambda _d: {p: PageClassification.OCR_REQUIRED for p in range(1, 6)},
    )
    monkeypatch.setattr(pdf_ocr, "render_pdf_page", lambda *a, **k: (b"", 1, 1, 1, 1))
    monkeypatch.setattr(pdf_ocr, "get_tesseract_version", lambda: "t")

    def slow_ocr(_png, **_k):
        time.sleep(0.2)
        return types.SimpleNamespace(words=[1], elements=[])

    monkeypatch.setattr(pdf_ocr, "ocr_page_image", slow_ocr)

    async def heartbeat(ticks: list[int]) -> None:
        while True:
            await asyncio.sleep(0.05)
            ticks.append(1)

    async def run() -> int:
        ticks: list[int] = []
        beat = asyncio.create_task(heartbeat(ticks))
        await pdf_ocr.process_pdf_with_ocr(b"x")
        beat.cancel()
        return len(ticks)

    # 5 pages x 0.2 s is about 20 ticks when the loop is free and 0 when OCR blocks it.
    assert asyncio.run(run()) >= 10
