"""MIME -> DocumentParser registry (M2-001c).

Keeps parser construction in one place so the worker pipeline (and the upload
endpoint's mime validation) share it without importing individual adapters.
"""

from __future__ import annotations

from ..interfaces import DocumentParser
from .csv import CsvParser
from .docling import DoclingParser
from .docx import DocxParser
from .html import HtmlParser
from .markdown import MarkdownParser
from .pdf_table import PdfTableParser
from .pdf_text import PdfTextParser
from .txt import TextParser

# T-V1-030 — accepted parser precedence, recorded first in DECISIONS.md
# ("T-V1-030 parser precedence" row) and pinned by
# tests/unit/test_parser_precedence.py. It reconciles frozen PD-001
# (pypdf/pdfplumber primary, decisions/PD-001-v1-parser-stack.md) with
# V1-COMPLETION-PLAN §2.5 "Docling-first with fallback", per the AGENTS.md
# source-of-truth priority (DECISIONS.md outranks PLAN) and the C19 ruling
# (docling is covered by DECISIONS.md; PD-001 stays frozen):
#   1. Docling-first: DoclingParser is PRIMARY for the layout formats
#      DECISIONS.md declares docling primary for and that docling serves in
#      this deployment — PDF, DOCX, HTML.
#   2. PD-001 fast path stays PRIMARY for the plain-text family (markdown,
#      txt, csv): docling cannot parse text/* here (its `marko` format extra
#      is absent) and adds nothing for plain text, so docling-first on those
#      mimes would fail every TXT/MD upload.
#   3. Registration stays first-wins, so a mime resolves to its accepted
#      primary and every later claimant stays a FALLBACK: PARSER_PRECEDENCE
#      holds the full chain in accepted order (pdf -> docling -> pypdf ->
#      pdfplumber, etc.), disclosed in DECISIONS.md and available to a
#      runtime fallback router (T-V1-031). parser_for_mime returns the
#      primary only; fallbacks also remain directly importable.
DOCLING_PRIMARY_MIMES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        "text/html",
    }
)

_DOCLING = DoclingParser()

_PARSERS: tuple[DocumentParser, ...] = (
    _DOCLING,
    PdfTextParser(),
    PdfTableParser(),
    DocxParser(),
    HtmlParser(),
    MarkdownParser(),
    CsvParser(),
    TextParser(),
)

PARSER_REGISTRY: dict[str, DocumentParser] = {}
PARSER_PRECEDENCE: dict[str, tuple[DocumentParser, ...]] = {}
for _parser in _PARSERS:
    for _mime in _parser.supported_mime_types:
        if _parser is _DOCLING and _mime not in DOCLING_PRIMARY_MIMES:
            continue  # docling claims text/* it cannot serve here -> PD-001 fast path
        _chain = PARSER_PRECEDENCE.get(_mime, ())
        PARSER_PRECEDENCE[_mime] = (*_chain, _parser)
        PARSER_REGISTRY.setdefault(_mime, _parser)


def parser_for_mime(mime_type: str | None) -> DocumentParser | None:
    if not mime_type:
        return None
    return PARSER_REGISTRY.get(mime_type)


def supported_mime_types() -> frozenset[str]:
    return frozenset(PARSER_REGISTRY)
