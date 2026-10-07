"""Unit tests for the PD-001 parser adapters (M2-001a).

Positive per-format assertions for all six fixtures live in the M0-004 contract
test (`tests/contracts/test_document_parser_contract.py`, driven by
`expected_elements.json`); this file adds the negative matrix (corrupt PDF,
MIME-spoofed bytes, oversized/empty input), the TXT adapter (no manifest
fixture exists yet), and a few direct element assertions.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion.adapters.csv import PARSER as CSV
from ingestion.adapters.docx import PARSER as DOCX
from ingestion.adapters.html import PARSER as HTML
from ingestion.adapters.markdown import PARSER as MARKDOWN
from ingestion.adapters.pdf_table import PARSER as PDF_TABLE
from ingestion.adapters.pdf_text import PARSER as PDF_TEXT
from ingestion.adapters.txt import PARSER as TEXT
from ingestion.normalize import (
    DocumentTooLargeError,
    EmptyDocumentError,
    ParseError,
    UnsupportedMimeTypeError,
    validate_input,
)

pytestmark = pytest.mark.unit

DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
MAX = 25 * 1024 * 1024


def _fixture(fixtures_dir: Path, name: str) -> bytes:
    return (fixtures_dir / "pd001" / name).read_bytes()


# ---- TXT (stdlib + charset-normalizer) --------------------------------------


async def test_txt_splits_paragraphs() -> None:
    document = await TEXT.parse(b"First paragraph.\n\nSecond paragraph.", mime_type="text/plain")
    assert [element.type.value for element in document.elements] == ["text", "text"]
    assert document.elements[1].text == "Second paragraph."


async def test_txt_decodes_latin1() -> None:
    document = await TEXT.parse("Olá ção, teste".encode("latin-1"), mime_type="text/plain")
    assert "teste" in document.elements[0].text


# ---- negative matrix --------------------------------------------------------


def test_validate_input_raises_typed_errors() -> None:
    with pytest.raises(UnsupportedMimeTypeError):
        validate_input(b"x", "audio/mpeg", frozenset({"text/plain"}))
    with pytest.raises(EmptyDocumentError):
        validate_input(b"", "text/plain", frozenset({"text/plain"}))
    with pytest.raises(DocumentTooLargeError):
        validate_input(b"xxxxx", "text/plain", frozenset({"text/plain"}), max_bytes=4)


async def test_corrupt_pdf_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        await PDF_TEXT.parse(b"%PDF-1.4\nthis is not a real pdf", mime_type="application/pdf")


async def test_mime_spoofed_html_as_pdf_raises() -> None:
    with pytest.raises(ParseError):
        await PDF_TEXT.parse(
            b"<html><body>spoofed</body></html>", mime_type="application/pdf"
        )


async def test_empty_document_rejected_by_parser() -> None:
    with pytest.raises(EmptyDocumentError):
        await CSV.parse(b"", mime_type="text/csv")


async def test_oversized_document_rejected_by_parser() -> None:
    with pytest.raises(DocumentTooLargeError):
        await TEXT.parse(b"x" * (MAX + 1), mime_type="text/plain")


# ---- direct element assertions (beyond the manifest) ------------------------


async def test_pdf_text_preserves_pages_and_sections(fixtures_dir: Path) -> None:
    document = await PDF_TEXT.parse(
        _fixture(fixtures_dir, "pdf_text_multipage.pdf"), mime_type="application/pdf"
    )
    assert {element.page for element in document.elements} == {1, 2, 3}
    assert any(
        element.section == "Introduction" and element.page == 1 for element in document.elements
    )
    assert any(
        element.section == "Concluzii" and element.page == 3 for element in document.elements
    )
    assert any(element.type.value == "title" for element in document.elements)


async def test_pdf_table_keeps_tables_not_text_soup(fixtures_dir: Path) -> None:
    document = await PDF_TABLE.parse(
        _fixture(fixtures_dir, "pdf_table.pdf"), mime_type="application/pdf"
    )
    tables = [element for element in document.elements if element.type.value == "table"]
    assert len(tables) == 2
    assert all("Revenue" in table.text for table in tables)
    assert {table.section for table in tables} == {"Quarterly Numbers", "Half-year Notes"}


async def test_csv_preserves_quoted_commas_and_language(fixtures_dir: Path) -> None:
    document = await CSV.parse(_fixture(fixtures_dir, "csv_multilingual.csv"), mime_type="text/csv")
    assert [element.metadata.get("language") for element in document.elements] == [
        "ro",
        "pt",
        "en",
        "ro",
    ]
    assert any("Ana are, mere, și pere" in element.text for element in document.elements)


async def test_markdown_keeps_code_and_lists(fixtures_dir: Path) -> None:
    document = await MARKDOWN.parse(
        _fixture(fixtures_dir, "markdown_structure.md"), mime_type="text/markdown"
    )
    assert any(
        element.type.value == "code" and "def fuse" in element.text
        for element in document.elements
    )
    assert any(
        element.type.value == "list" and "primul element" in element.text
        for element in document.elements
    )


async def test_html_strips_boilerplate(fixtures_dir: Path) -> None:
    document = await HTML.parse(
        _fixture(fixtures_dir, "html_article_nav.html"), mime_type="text/html"
    )
    joined = " ".join(element.text for element in document.elements)
    assert "Cookie policy" not in joined
    assert "analyticsQueue" not in joined
    assert "Copyright 2026" not in joined
    assert document.language == "en"


async def test_docx_table_and_heading_sections(fixtures_dir: Path) -> None:
    document = await DOCX.parse(
        _fixture(fixtures_dir, "docx_headings_table.docx"), mime_type=DOCX_MIME
    )
    assert any(
        element.type.value == "table"
        and "Revenue" in element.text
        and element.section == "Secção mensal"
        for element in document.elements
    )
