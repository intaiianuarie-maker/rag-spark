"""T-V1-030 — pin the accepted parser precedence (primary + fallback order).

The decision is recorded first in `DECISIONS.md` ("T-V1-030 parser
precedence" row) and reconciles frozen PD-001 (pypdf/pdfplumber primary) with
V1-COMPLETION-PLAN §2.5 "Docling-first with fallback". This file is the pin:
if anyone re-orders `_PARSERS` or widens docling's claims, the accepted
primary/fallback order breaks here instead of silently changing what the
worker parses.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion.adapters import registry
from ingestion.adapters.csv import CsvParser
from ingestion.adapters.docling import DoclingParser
from ingestion.adapters.docx import DocxParser
from ingestion.adapters.html import HtmlParser
from ingestion.adapters.markdown import MarkdownParser
from ingestion.adapters.pdf_table import PdfTableParser
from ingestion.adapters.pdf_text import PdfTextParser
from ingestion.adapters.txt import TextParser
from ingestion.interfaces import DocumentParser

pytestmark = pytest.mark.unit

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
HTML = "text/html"
XHTML = "application/xhtml+xml"
MD = "text/markdown"
MD_ALT = "text/x-markdown"
TXT = "text/plain"
CSV = "text/csv"
CSV_ALT = "application/csv"

#: The complete accepted precedence table: mime -> parsers in order
#: (primary first, fallbacks after). Every entry is pinned, not sampled.
ACCEPTED: dict[str, tuple[type[DocumentParser], ...]] = {
    PDF: (DoclingParser, PdfTextParser, PdfTableParser),
    DOCX: (DoclingParser, DocxParser),
    DOCX_MAIN: (DoclingParser, DocxParser),
    HTML: (DoclingParser, HtmlParser),
    XHTML: (HtmlParser,),
    MD: (MarkdownParser,),
    MD_ALT: (MarkdownParser,),
    TXT: (TextParser,),
    CSV: (CsvParser,),
    CSV_ALT: (CsvParser,),
}


def _chain(mime: str) -> tuple[type[DocumentParser], ...]:
    return tuple(type(parser) for parser in registry.PARSER_PRECEDENCE[mime])


def test_docling_first_for_the_layout_formats() -> None:
    """Docling is PRIMARY for PDF/DOCX/HTML — the completion plan's docling-first."""
    for mime in (PDF, DOCX, DOCX_MAIN, HTML):
        assert _chain(mime)[0] is DoclingParser, mime
        assert type(registry.parser_for_mime(mime)) is DoclingParser, mime


def test_pd001_fast_path_stays_primary_for_the_plain_text_family() -> None:
    """PD-001 parsers stay PRIMARY where docling cannot serve (text/*) or does not claim."""
    for mime in (MD, MD_ALT, TXT, CSV, CSV_ALT, XHTML):
        expected = ACCEPTED[mime]
        assert len(expected) == 1, mime
        assert type(registry.parser_for_mime(mime)) is expected[0], mime


def test_every_mimes_fallback_order_is_pinned() -> None:
    """The full chain, in accepted order — this is the fallback disclosure."""
    assert set(registry.PARSER_PRECEDENCE) == set(ACCEPTED)
    for mime, expected in ACCEPTED.items():
        assert _chain(mime) == expected, mime


def test_docling_cannot_displace_the_text_family() -> None:
    """Docling's claims are scoped: it never owns text/plain or text/markdown."""
    claimed = DoclingParser().supported_mime_types
    assert registry.DOCLING_PRIMARY_MIMES <= claimed
    assert registry.DOCLING_PRIMARY_MIMES.isdisjoint({MD, MD_ALT, TXT})
    for mime in (MD, MD_ALT, TXT, CSV, CSV_ALT, XHTML):
        assert not any(
            parser is registry._DOCLING for parser in registry.PARSER_PRECEDENCE[mime]
        ), mime


def test_primary_is_always_the_first_claimant() -> None:
    """Registration stays first-wins: registry entry == head of the chain."""
    for mime, chain in registry.PARSER_PRECEDENCE.items():
        assert registry.PARSER_REGISTRY[mime] is chain[0], mime
    assert registry._PARSERS[0] is registry._DOCLING  # docling listed first


def test_supported_mime_surface_is_unchanged() -> None:
    """Precedence scoping changes who wins, never which mimes are accepted."""
    assert registry.supported_mime_types() == frozenset(ACCEPTED)


def test_unknown_and_empty_mime_still_resolve_to_nothing() -> None:
    assert registry.parser_for_mime(None) is None
    assert registry.parser_for_mime("") is None
    assert registry.parser_for_mime("audio/mpeg") is None


def test_fallbacks_remain_directly_importable() -> None:
    """Disclosed fallback: every chain member is importable without the registry."""
    for chain in ACCEPTED.values():
        for parser_type in chain:
            assert issubclass(parser_type, DocumentParser)
