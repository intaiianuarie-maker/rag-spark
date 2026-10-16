"""Orchestrator for conditional PDF OCR routing and element merging (PD-001).

Classifies PDF pages into native_text, ocr_required, blank, or uncertain.
Routes readable pages to the native parser chain and scanned pages to PDFium
rasterization + Tesseract 5 OCR. Merges normalized elements in reading order
and produces an ExtractionReport with provenance.
"""

from __future__ import annotations

import hashlib
import time
from io import BytesIO
from typing import Any

from pypdf import PdfReader

from .adapters.pdf_scan import _page_has_image
from .adapters.pdfium_render import render_pdf_page
from .adapters.registry import parse_with_precedence
from .adapters.tesseract import (
    DEFAULT_LANGUAGES,
    DEFAULT_PSM,
    DEFAULT_TIMEOUT_SECONDS,
    get_tesseract_version,
    ocr_page_image,
)
from .interfaces import (
    DocumentElement,
    ExtractionReport,
    PageClassification,
)
from .metadata import detect_language

_MIN_NATIVE_CHARS = 20
_MIN_NATIVE_WORDS = 4


def classify_page(page: Any, page_number: int) -> PageClassification:
    """Classify a single PDF page."""
    try:
        text = (page.extract_text() or "").strip()
        words = text.split()
        has_image = _page_has_image(page)

        has_meaningful_text = len(text) >= _MIN_NATIVE_CHARS and len(words) >= _MIN_NATIVE_WORDS

        if has_meaningful_text:
            return PageClassification.NATIVE_TEXT
        if has_image:
            return PageClassification.OCR_REQUIRED
        if not text and not has_image:
            return PageClassification.BLANK
        return PageClassification.UNCERTAIN
    except Exception:
        return PageClassification.UNCERTAIN


def classify_pdf_pages(data: bytes) -> dict[int, PageClassification]:
    """Inspect and classify all pages of a PDF document (1-indexed)."""
    classifications: dict[int, PageClassification] = {}
    try:
        reader = PdfReader(BytesIO(data))
        for page_number, page in enumerate(reader.pages, start=1):
            classifications[page_number] = classify_page(page, page_number)
    except Exception:
        # If inspection fails entirely, return empty classification
        return {}
    return classifications


async def process_pdf_with_ocr(
    data: bytes,
    *,
    languages: str = DEFAULT_LANGUAGES,
    dpi: int = 300,
    psm: int = DEFAULT_PSM,
    max_pages: int = 50,
    timeout_per_page: float = DEFAULT_TIMEOUT_SECONDS,
    filename: str | None = None,
) -> tuple[list[DocumentElement], ExtractionReport]:
    """Run conditional OCR on a PDF document.

    Routes native pages to the standard parser chain and scanned pages to
    PDFium + Tesseract, then merges all elements in original page order.
    """
    start_time = time.monotonic()
    classifications = classify_pdf_pages(data)
    total_pages = len(classifications)

    native_pages: list[int] = []
    ocr_pages_todo: list[int] = []
    blank_pages: list[int] = []
    uncertain_pages: list[int] = []

    for page_num, classification in classifications.items():
        if classification == PageClassification.NATIVE_TEXT:
            native_pages.append(page_num)
        elif classification == PageClassification.OCR_REQUIRED:
            ocr_pages_todo.append(page_num)
        elif classification == PageClassification.BLANK:
            blank_pages.append(page_num)
        else:
            uncertain_pages.append(page_num)

    all_elements: list[DocumentElement] = []
    successful_ocr_pages: list[int] = []
    failed_pages: list[int] = []

    # 1. Parse native pages if any exist
    if native_pages:
        try:
            parsed_native, _report = await parse_with_precedence(
                data,
                mime_type="application/pdf",
                filename=filename,
            )
            # Filter elements to only keep those belonging to native_pages
            for element in parsed_native.elements:
                if element.page is None or element.page in native_pages:
                    all_elements.append(element)
        except Exception:
            # If native parse failed, fall back to marking native pages for OCR or failed
            for p in native_pages:
                if p not in ocr_pages_todo:
                    ocr_pages_todo.append(p)
            ocr_pages_todo.sort()
            native_pages.clear()

    # 2. Process OCR pages
    engine_ver = get_tesseract_version()
    for page_num in ocr_pages_todo[:max_pages]:
        try:
            png_bytes, w_pts, h_pts, pix_w, pix_h = render_pdf_page(
                data,
                page_num,
                dpi=dpi,
            )
            page_result = ocr_page_image(
                png_bytes,
                page_number=page_num,
                width_pts=w_pts,
                height_pts=h_pts,
                pixel_width=pix_w,
                pixel_height=pix_h,
                languages=languages,
                psm=psm,
                timeout_seconds=timeout_per_page,
            )
            if page_result.words:
                successful_ocr_pages.append(page_num)
                all_elements.extend(page_result.elements)
            else:
                # No text recognized on this image page
                failed_pages.append(page_num)
        except Exception:
            failed_pages.append(page_num)

    # If document exceeded max_pages limit for OCR
    if len(ocr_pages_todo) > max_pages:
        failed_pages.extend(ocr_pages_todo[max_pages:])

    # 3. Sort merged elements in reading order: page number, then vertical coordinate
    def _element_sort_key(el: DocumentElement) -> tuple[int, float]:
        page = el.page if el.page is not None else 0
        y0 = el.bbox.y0 if el.bbox is not None else 0.0
        return (page, y0)

    all_elements.sort(key=_element_sort_key)

    # 4. Determine overall detected languages
    sample_text = " ".join(el.text for el in all_elements[:20])
    detected_lang = detect_language(sample_text) if sample_text else None
    detected_langs = [detected_lang] if detected_lang else []

    profile_content = f"{languages}:{dpi}:{psm}:{engine_ver}".encode()
    profile_hash = hashlib.sha256(profile_content).hexdigest()[:16]

    elapsed = time.monotonic() - start_time

    report = ExtractionReport(
        native_pages=native_pages,
        ocr_pages=successful_ocr_pages,
        blank_pages=blank_pages,
        uncertain_pages=uncertain_pages,
        failed_pages=failed_pages,
        languages=detected_langs,
        engine="tesseract",
        engine_version=engine_ver,
        profile_hash=profile_hash,
        total_pages=total_pages,
        elapsed_seconds=elapsed,
    )

    return all_elements, report
