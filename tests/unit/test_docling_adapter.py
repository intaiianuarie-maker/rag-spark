"""Unit test for docling parser adapter (TASK-M2-001).

PDF + DOCX fixtures produce elements with page/section/bbox preserved,
language tagged, and no docling types leak past the adapter boundary.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion.adapters.docling import PARSER as DOCLING
from ingestion.interfaces import BBox, DocumentElement, ParsedDocument

pytestmark = pytest.mark.unit


def _fixture(fixtures_dir: Path, name: str) -> bytes:
    return (fixtures_dir / "pd001" / name).read_bytes()


async def test_docling_pdf_produces_elements_with_page_section_bbox(fixtures_dir: Path) -> None:
    data = _fixture(fixtures_dir, "pdf_text_multipage.pdf")
    doc = await DOCLING.parse(data, mime_type="application/pdf", filename="sample.pdf")

    assert isinstance(doc, ParsedDocument)
    assert doc.page_count is not None and doc.page_count >= 1
    assert len(doc.elements) >= 3
    # All elements are our types, not docling types
    assert all(isinstance(e, DocumentElement) for e in doc.elements)
    # Page numbers preserved where available
    assert any(e.page is not None for e in doc.elements)
    # Section tracking: at least one element has section set (from heading)
    # For this fixture, we expect at least 2 sections via numbered headings
    assert any(e.section is not None for e in doc.elements)
    # BBox preserved for at least some elements (pdf provides bbox)
    assert any(e.bbox is not None and isinstance(e.bbox, BBox) for e in doc.elements)
    # Language tagged
    assert doc.language in {"en", "ro", "pt", "unknown"} and doc.language is not None
    # No docling types leak: element text and metadata are plain python types
    for e in doc.elements:
        assert isinstance(e.text, str)
        assert isinstance(e.metadata, dict)
        # Ensure no docling-specific attributes leaked
        assert not hasattr(e, "prov")


async def test_docling_docx_produces_elements_with_section(fixtures_dir: Path) -> None:
    data = _fixture(fixtures_dir, "docx_headings_table.docx")
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    doc = await DOCLING.parse(data, mime_type=mime, filename="sample.docx")

    assert isinstance(doc, ParsedDocument)
    assert len(doc.elements) >= 3
    assert all(isinstance(e, DocumentElement) for e in doc.elements)
    # DOCX has no pagination, but sections from headings should be present
    assert any(e.section is not None for e in doc.elements)
    assert any(e.type.value == "heading" for e in doc.elements)
    assert doc.language is not None
