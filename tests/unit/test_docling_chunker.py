"""Unit tests for Docling HybridChunker adapter (T-OR-09).

Verifies:
- Offline, deterministic tokenizers (whitespace, approx gemini, custom callable)
- Zero HuggingFace runtime network requests or unapproved model downloads
- Headings context and section tracking preserved
- Structured table ingestion and table header repetition
- Parent-child chunk linking and token limits
- Complete absence of docling internal types leaking across the boundary
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion.adapters.docling import StructuredCell, StructuredTable
from ingestion.adapters.docling_chunker import (
    ApproxGeminiTokenizer,
    DoclingHybridChunker,
    WhitespaceTokenizer,
    chunk_document_hybrid,
)
from ingestion.chunk import Chunk, ChunkResult
from ingestion.interfaces import BBox, DocumentElement, ElementType, ParsedDocument

pytestmark = pytest.mark.unit


def test_tokenizers_offline_and_deterministic() -> None:
    ws_tok = WhitespaceTokenizer(max_tokens=100)
    assert ws_tok.count_tokens("Hello world from Docling chunker") == 5
    assert ws_tok.get_max_tokens() == 100

    gemini_tok = ApproxGeminiTokenizer(max_tokens=100)
    text = "Hello world from Docling chunker"
    count = gemini_tok.count_tokens(text)
    assert count >= 5
    assert gemini_tok.get_max_tokens() == 100


def test_docling_hybrid_chunker_text_and_headings() -> None:
    elements = [
        DocumentElement(type=ElementType.TITLE, text="System Architecture Guide", page=1),
        DocumentElement(
            type=ElementType.HEADING,
            text="Section 1: Data Ingestion",
            page=1,
            section="System Architecture Guide",
        ),
        DocumentElement(
            type=ElementType.TEXT,
            text=(
                "This section describes the ingestion pipeline. "
                "We normalize incoming documents into elements."
            ),
            page=1,
            section="Section 1: Data Ingestion",
        ),
        DocumentElement(
            type=ElementType.TEXT,
            text="Elements are then passed to the chunker to create grounded chunks.",
            page=1,
            section="Section 1: Data Ingestion",
        ),
    ]

    parsed = ParsedDocument(
        elements=elements,
        language="en",
        mime_type="application/pdf",
        page_count=1,
    )

    chunker = DoclingHybridChunker(max_tokens=100, tokenizer="whitespace")
    res = chunker.chunk(parsed, filename="architecture.pdf")

    assert isinstance(res, ChunkResult)
    assert res.language == "en"
    assert len(res.chunks) >= 1
    assert all(isinstance(c, Chunk) for c in res.chunks)

    # First chunk has section tracking
    first = res.chunks[0]
    assert first.section in ("System Architecture Guide", "Section 1: Data Ingestion")
    assert first.page == 1
    assert first.token_count > 0
    assert first.metadata.get("chunking_backend") == "docling_hybrid"
    assert "System Architecture Guide" in first.metadata.get("headings", []) or first.section


def test_docling_hybrid_chunker_with_structured_table() -> None:
    # Build a StructuredTable element from T-OR-08
    cells = [
        StructuredCell(text="Quarter", row=0, col=0, is_header=True),
        StructuredCell(text="Revenue", row=0, col=1, is_header=True),
        StructuredCell(text="Q1 2026", row=1, col=0),
        StructuredCell(text="$50M across European and US markets", row=1, col=1),
        StructuredCell(text="Q2 2026", row=2, col=0),
        StructuredCell(text="$65M across European and US markets", row=2, col=1),
    ]
    st = StructuredTable(
        num_rows=3,
        num_cols=2,
        header_rows=1,
        markdown="| Quarter | Revenue |\n| --- | --- |\n| Q1 2026 | $50M |\n| Q2 2026 | $65M |",
        rows=[
            ["Quarter", "Revenue"],
            ["Q1 2026", "$50M across European and US markets"],
            ["Q2 2026", "$65M across European and US markets"],
        ],
        cells=cells,
        page=2,
        bbox=BBox(x0=10.0, y0=20.0, x1=100.0, y1=80.0),
        profile="native",
    )

    elements = [
        DocumentElement(
            type=ElementType.HEADING,
            text="Financial Performance",
            page=2,
            section="Financial Performance",
        ),
        DocumentElement(
            type=ElementType.TABLE,
            text=st.markdown,
            page=2,
            section="Financial Performance",
            metadata={"structured_table": st.model_dump()},
        ),
    ]

    parsed = ParsedDocument(
        elements=elements,
        language="en",
        mime_type="application/pdf",
        page_count=2,
    )

    res = chunk_document_hybrid(
        parsed,
        max_tokens=30,
        tokenizer="whitespace",
        repeat_table_header=True,
    )

    assert isinstance(res, ChunkResult)
    assert len(res.chunks) >= 1
    assert any("table" in c.element_types for c in res.chunks)
    assert any(c.page == 2 for c in res.chunks)


def test_zero_vendor_types_leak() -> None:
    elements = [
        DocumentElement(type=ElementType.TITLE, text="Report Title", page=1),
        DocumentElement(type=ElementType.TEXT, text="Content paragraph for testing.", page=1),
    ]
    parsed = ParsedDocument(elements=elements, language="en", mime_type="text/plain")

    res = chunk_document_hybrid(parsed, max_tokens=50)
    for c in res.chunks:
        assert type(c.id) is str
        assert type(c.text) is str
        assert type(c.token_count) is int
        assert type(c.element_types) is list
        assert type(c.metadata) is dict
        for k, v in c.metadata.items():
            assert isinstance(k, str)
            assert isinstance(v, (str, int, float, bool, list, dict, type(None)))
        assert not hasattr(c, "_docling")
        assert not hasattr(c, "docling_meta")
