"""Unit test suite for DoclingParser with structured table and profile support (T-OR-08).

Verifies:
- StructuredCell and StructuredTable models and field serialization
- DoclingParser profile configuration (native vs tableformer/structured)
- Subprocess/memory cap configuration (default 2048 MB)
- Preserving cell rows/cols, markdown, headers, and bounding boxes
- Graceful fallback with disclosed limitations when structured models are absent
- Zero docling vendor types leaked past the adapter boundary
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion.adapters.docling import (
    DoclingParser,
    StructuredCell,
    StructuredTable,
    create_parser,
)
from ingestion.interfaces import BBox, DocumentElement, ElementType, ParsedDocument

pytestmark = pytest.mark.unit


def _fixture(fixtures_dir: Path, name: str) -> bytes:
    return (fixtures_dir / "pd001" / name).read_bytes()


def test_structured_cell_and_table_models() -> None:
    cell1 = StructuredCell(
        text="Quarter",
        row=0,
        col=0,
        row_span=1,
        col_span=1,
        is_header=True,
        bbox=BBox(x0=10.0, y0=20.0, x1=50.0, y1=40.0),
    )
    cell2 = StructuredCell(
        text="Revenue",
        row=0,
        col=1,
        row_span=1,
        col_span=1,
        is_header=True,
    )
    cell3 = StructuredCell(
        text="Q1",
        row=1,
        col=0,
        is_header=False,
    )
    cell4 = StructuredCell(
        text="$1.2M",
        row=1,
        col=1,
        is_header=False,
    )

    table = StructuredTable(
        num_rows=2,
        num_cols=2,
        header_rows=1,
        markdown="| Quarter | Revenue |\n| --- | --- |\n| Q1 | $1.2M |",
        rows=[["Quarter", "Revenue"], ["Q1", "$1.2M"]],
        cells=[cell1, cell2, cell3, cell4],
        page=1,
        bbox=BBox(x0=10.0, y0=20.0, x1=100.0, y1=80.0),
        profile="native",
    )

    dumped = table.model_dump()
    assert dumped["num_rows"] == 2
    assert dumped["num_cols"] == 2
    assert len(dumped["cells"]) == 4
    assert dumped["cells"][0]["is_header"] is True
    assert dumped["cells"][0]["bbox"] == {"x0": 10.0, "y0": 20.0, "x1": 50.0, "y1": 40.0}
    assert dumped["profile"] == "native"

    # Round-trip validation
    rehydrated = StructuredTable.model_validate(dumped)
    assert rehydrated.num_rows == 2
    assert rehydrated.cells[1].text == "Revenue"


def test_parser_factory_and_defaults() -> None:
    default_parser = create_parser()
    assert isinstance(default_parser, DoclingParser)
    assert default_parser.profile == "native"
    assert default_parser.max_child_memory_mb == 2048

    custom_parser = create_parser(profile="tableformer", max_child_memory_mb=1024)
    assert custom_parser.profile == "tableformer"
    assert custom_parser.max_child_memory_mb == 1024


async def test_native_pdf_parsing_preserves_metadata(fixtures_dir: Path) -> None:
    parser = DoclingParser(profile="native")
    data = _fixture(fixtures_dir, "pdf_text_multipage.pdf")
    doc = await parser.parse(data, mime_type="application/pdf", filename="sample.pdf")

    assert isinstance(doc, ParsedDocument)
    assert doc.metadata.get("docling_profile") == "native"
    assert "docling_profile_limitation" not in doc.metadata
    assert doc.page_count is not None and doc.page_count >= 1
    assert len(doc.elements) >= 3
    assert all(isinstance(e, DocumentElement) for e in doc.elements)


async def test_structured_table_extraction_with_mock_grid(fixtures_dir: Path) -> None:
    """Verify that when Docling yields tables with grid data, StructuredTable is populated."""
    parser = DoclingParser(profile="native")
    data = _fixture(fixtures_dir, "pdf_text_multipage.pdf")

    # Mock docling converter result with a mock table containing grid cells
    mock_cell_a = MagicMock()
    mock_cell_a.text = "Header A"
    mock_cell_a.start_row_offset_idx = 0
    mock_cell_a.start_col_offset_idx = 0
    mock_cell_a.row_span = 1
    mock_cell_a.col_span = 1
    mock_cell_a.column_header = True
    mock_cell_a.row_header = False
    mock_cell_a.bbox = MagicMock(l=10, b=20, r=30, t=40)

    mock_cell_b = MagicMock()
    mock_cell_b.text = "Value 1"
    mock_cell_b.start_row_offset_idx = 1
    mock_cell_b.start_col_offset_idx = 0
    mock_cell_b.row_span = 1
    mock_cell_b.col_span = 1
    mock_cell_b.column_header = False
    mock_cell_b.row_header = False
    mock_cell_b.bbox = MagicMock(l=10, b=5, r=30, t=19)

    mock_table = MagicMock()
    mock_table.data.grid = [[mock_cell_a], [mock_cell_b]]
    mock_table.export_to_markdown.return_value = "| Header A |\n| --- |\n| Value 1 |"
    prov_mock = MagicMock()
    prov_mock.page_no = 1
    prov_mock.bbox = MagicMock(l=10, b=5, r=30, t=40)
    mock_table.prov = [prov_mock]

    mock_doc = MagicMock()
    mock_doc.texts = []
    mock_doc.tables = [mock_table]

    with patch("docling.document_converter.DocumentConverter.convert") as mock_convert:
        mock_convert.return_value.document = mock_doc
        parsed = await parser.parse(data, mime_type="application/pdf")

    assert len(parsed.elements) == 1
    el = parsed.elements[0]
    assert el.type == ElementType.TABLE
    assert "structured_table" in el.metadata

    structured = StructuredTable.model_validate(el.metadata["structured_table"])
    assert structured.num_rows == 2
    assert structured.num_cols == 1
    assert structured.header_rows == 1
    assert len(structured.cells) == 2
    assert structured.cells[0].is_header is True
    assert structured.cells[0].text == "Header A"
    assert structured.cells[1].text == "Value 1"
    assert structured.profile == "native"


async def test_tableformer_fallback_with_disclosed_limitation(fixtures_dir: Path) -> None:
    """Verify that when tableformer profile fails or is missing assets,
    it falls back to native with disclosure.
    """
    parser = DoclingParser(profile="tableformer")
    data = _fixture(fixtures_dir, "pdf_text_multipage.pdf")

    # If PdfPipelineOptions or converter fails on structured pipeline, it falls back to native
    doc = await parser.parse(data, mime_type="application/pdf", filename="sample.pdf")
    assert isinstance(doc, ParsedDocument)
    assert len(doc.elements) >= 1
    # Profile limitation should be disclosed if fallback happened or native profile was used
    assert doc.metadata.get("docling_profile") == "tableformer"
    # Limitation disclosure explains fallback
    assert "docling_profile_limitation" in doc.metadata
    assert "tableformer" in doc.metadata["docling_profile_limitation"]


async def test_zero_vendor_types_leak(fixtures_dir: Path) -> None:
    parser = DoclingParser()
    data = _fixture(fixtures_dir, "pdf_text_multipage.pdf")
    doc = await parser.parse(data, mime_type="application/pdf")

    for el in doc.elements:
        assert type(el.text) is str
        assert type(el.metadata) is dict
        for k, v in el.metadata.items():
            # Ensure metadata keys and values are standard JSON-serializable types
            assert isinstance(k, str)
            assert isinstance(v, (str, int, float, bool, list, dict, type(None)))
        assert not hasattr(el, "prov")
        assert not hasattr(el, "_docling")
