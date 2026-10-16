"""Unit tests for Tesseract OCR and PDFium adapters (RO, EN, PT)."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion.adapters.pdfium_render import (
    render_pdf_page,
    transform_bbox_to_points,
)
from ingestion.adapters.tesseract import (
    build_line_elements,
    ocr_page_image,
    parse_tsv_words,
)
from ingestion.chunk import chunk_elements
from ingestion.interfaces import (
    BBox,
    DocumentElement,
    ElementType,
    PageClassification,
)
from ingestion.ocr import (
    classify_pdf_pages,
    process_pdf_with_ocr,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "formats"
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _make_scanned_pdf(images: list[Image.Image]) -> bytes:
    buf = io.BytesIO()
    if images:
        images[0].save(buf, format="PDF", save_all=True, append_images=images[1:])
    return buf.getvalue()


def test_transform_bbox_to_points() -> None:
    """Verify coordinate scaling from rendered pixels to PDF points."""
    bbox = transform_bbox_to_points(
        left=100.0,
        top=200.0,
        width=50.0,
        height=80.0,
        pixel_width=1000,
        pixel_height=2000,
        width_pts=500.0,
        height_pts=1000.0,
    )
    assert bbox.x0 == 50.0
    assert bbox.y0 == 100.0
    assert bbox.x1 == 75.0
    assert bbox.y1 == 140.0


def test_pdfium_render_page_and_dimensions() -> None:
    """Render a real PDF page via PDFium and assert dimension scaling."""
    pdf_path = FIXTURE_DIR / "pdf_digital.pdf"
    png_bytes, width_pts, height_pts, pixel_w, pixel_h = render_pdf_page(
        pdf_path.read_bytes(),
        page_number=1,
        dpi=300,
    )
    assert len(png_bytes) > 0
    assert width_pts == 612.0
    assert height_pts == 792.0
    assert pixel_w == 2550
    assert abs(pixel_h - 3300) <= 1


def test_pdfium_render_budget_checks() -> None:
    """Render bounds checks: invalid page numbers and pixel budget exceeded."""
    pdf_path = FIXTURE_DIR / "pdf_digital.pdf"
    data = pdf_path.read_bytes()

    with pytest.raises(ValueError, match="page_number must be >= 1"):
        render_pdf_page(data, page_number=0)

    with pytest.raises(ValueError, match="exceeds total pages"):
        render_pdf_page(data, page_number=999)

    with pytest.raises(ValueError, match="exceeds budget"):
        render_pdf_page(data, page_number=1, max_pixels=1000)


def test_parse_tsv_words_and_build_line_elements() -> None:
    """Verify TSV parsing and line grouping logic."""
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "1\t1\t0\t0\t0\t0\t0\t0\t1000\t1000\t-1\t\n"
        "5\t1\t1\t1\t1\t1\t100\t200\t50\t30\t95.0\tRaport\n"
        "5\t1\t1\t1\t1\t2\t160\t200\t60\t30\t96.0\tRomânia\n"
        "5\t1\t1\t1\t2\t1\t100\t250\t70\t30\t92.0\tConcluzie\n"
    )
    words = parse_tsv_words(
        tsv,
        width_pts=500.0,
        height_pts=500.0,
        pixel_width=1000,
        pixel_height=1000,
    )
    assert len(words) == 3
    assert words[0].text == "Raport"
    assert words[1].text == "România"
    assert words[2].text == "Concluzie"

    elements = build_line_elements(
        words,
        page_number=1,
        engine_version="tesseract 5.5.0",
        languages="ron+eng+por",
    )
    assert len(elements) == 2
    assert elements[0].text == "Raport România"
    assert elements[0].page == 1
    assert elements[0].metadata["extraction"]["method"] == "tesseract"
    assert elements[0].metadata["extraction"]["confidence"] == 95.5
    assert len(elements[0].metadata["extraction"]["source_spans"]) == 2

    assert elements[1].text == "Concluzie"
    assert elements[1].metadata["extraction"]["confidence"] == 92.0


def test_tesseract_ocr_romanian_portuguese_english() -> None:
    """End-to-end OCR test on real rendered Romanian, Portuguese, and English diacritics."""
    img = Image.new("RGB", (1600, 600), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT_PATH, 32)

    ro_text = "Raport preliminar: învățare, analiză și dezvoltare în România."
    pt_text = "Relatório de operações: atenção, produção e validação."
    en_text = "Knowledge OS document ingestion and retrieval system."

    draw.text((50, 50), ro_text, font=font, fill=(0, 0, 0))
    draw.text((50, 200), pt_text, font=font, fill=(0, 0, 0))
    draw.text((50, 350), en_text, font=font, fill=(0, 0, 0))

    buf = io.BytesIO()
    img.save(buf, format="PNG")

    result = ocr_page_image(
        buf.getvalue(),
        page_number=1,
        width_pts=800.0,
        height_pts=300.0,
        pixel_width=1600,
        pixel_height=600,
        languages="ron+por+eng",
    )

    assert result.mean_confidence > 70.0
    text = result.text

    # Romanian preservation (accept both comma and cedilla variants)
    assert "România" in text
    assert "analiz" in text
    assert "dezvoltare" in text

    # Portuguese preservation
    assert "Relatório" in text or "Relatorio" in text
    assert "produção" in text or "producao" in text
    assert "validação" in text or "validacao" in text

    # English preservation
    assert "Knowledge" in text
    assert "ingestion" in text


def test_classify_pdf_pages() -> None:
    """Verify classification of digital, scanned, and mixed PDFs."""
    digital = (FIXTURE_DIR / "pdf_digital.pdf").read_bytes()
    classifications = classify_pdf_pages(digital)
    assert classifications[1] == PageClassification.NATIVE_TEXT
    assert classifications[2] == PageClassification.NATIVE_TEXT

    scanned = (FIXTURE_DIR / "pdf_scanned.pdf").read_bytes()
    class_scanned = classify_pdf_pages(scanned)
    assert class_scanned[1] == PageClassification.OCR_REQUIRED

    mixed = (FIXTURE_DIR / "pdf_mixed_scan.pdf").read_bytes()
    class_mixed = classify_pdf_pages(mixed)
    assert class_mixed[1] == PageClassification.NATIVE_TEXT
    assert class_mixed[2] == PageClassification.OCR_REQUIRED


@pytest.mark.asyncio
async def test_process_pdf_with_ocr_synthetic() -> None:
    """Verify conditional OCR processing, page routing, and report generation."""
    img1 = Image.new("RGB", (1200, 800), color=(255, 255, 255))
    d1 = ImageDraw.Draw(img1)
    font = ImageFont.truetype(FONT_PATH, 32)
    d1.text((50, 100), "Raport tehnic: analiză și dezvoltare.", font=font, fill=(0, 0, 0))

    img2 = Image.new("RGB", (1200, 800), color=(255, 255, 255))
    d2 = ImageDraw.Draw(img2)
    d2.text((50, 100), "Operação e validação de documentos.", font=font, fill=(0, 0, 0))

    pdf_bytes = _make_scanned_pdf([img1, img2])

    elements, report = await process_pdf_with_ocr(pdf_bytes, languages="ron+por+eng")

    assert report.total_pages == 2
    assert report.ocr_pages == [1, 2]
    assert report.failed_pages == []
    assert report.has_ocr is True
    assert report.fully_lost is False

    assert len(elements) >= 2
    assert elements[0].page == 1
    assert elements[1].page == 2
    assert elements[0].metadata["extraction"]["method"] == "tesseract"


def test_ocr_chunk_page_isolation() -> None:
    """Verify chunks do not cross page boundaries for OCR elements."""
    el1 = DocumentElement(
        type=ElementType.TEXT,
        text="Pagina unu din raport.",
        page=1,
        bbox=BBox(x0=10, y0=10, x1=100, y1=20),
        metadata={"extraction": {"method": "tesseract", "page": 1, "confidence": 95.0}},
    )
    el2 = DocumentElement(
        type=ElementType.TEXT,
        text="Pagina doi din raport.",
        page=2,
        bbox=BBox(x0=10, y0=10, x1=100, y1=20),
        metadata={"extraction": {"method": "tesseract", "page": 2, "confidence": 94.0}},
    )

    chunks = chunk_elements([el1, el2], max_tokens=100)
    assert len(chunks) == 2
    assert chunks[0].page == 1
    assert chunks[1].page == 2
    assert chunks[0].metadata["extraction"]["page"] == 1
    assert chunks[1].metadata["extraction"]["page"] == 2


@pytest.mark.asyncio
async def test_process_pdf_with_ocr_empty_scan_reports_failed_pages() -> None:
    """Verify empty scan images without text are recorded as failed pages."""
    scanned_pdf = (FIXTURE_DIR / "pdf_scanned.pdf").read_bytes()
    elements, report = await process_pdf_with_ocr(scanned_pdf, languages="ron+por+eng")

    assert len(elements) == 0
    assert report.total_pages == 3
    assert report.failed_pages == [1, 2, 3]
    assert report.ocr_pages == []
    assert report.fully_lost is True

