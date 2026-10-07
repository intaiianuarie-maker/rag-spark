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

_PARSERS: tuple[DocumentParser, ...] = (
    PdfTextParser(),
    PdfTableParser(),
    DocxParser(),
    HtmlParser(),
    MarkdownParser(),
    CsvParser(),
    TextParser(),
    DoclingParser(),
)

# Preserve first-wins for existing mimes so docling does not override
# the PD-001 selected parsers; docling is available via direct import
# and as fallback for unclaimed mimes.
PARSER_REGISTRY: dict[str, DocumentParser] = {}
for _parser in _PARSERS:
    for _mime in _parser.supported_mime_types:
        PARSER_REGISTRY.setdefault(_mime, _parser)


def parser_for_mime(mime_type: str | None) -> DocumentParser | None:
    if not mime_type:
        return None
    return PARSER_REGISTRY.get(mime_type)


def supported_mime_types() -> frozenset[str]:
    return frozenset(PARSER_REGISTRY)
