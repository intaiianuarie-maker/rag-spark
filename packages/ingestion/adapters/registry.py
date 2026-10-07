"""MIME -> DocumentParser registry (M2-001c).

Keeps parser construction in one place so the worker pipeline (and the upload
endpoint's mime validation) share it without importing individual adapters.
"""

from __future__ import annotations

from ..interfaces import DocumentParser
from .csv import CsvParser
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
)

PARSER_REGISTRY: dict[str, DocumentParser] = {
    mime_type: parser
    for parser in _PARSERS
    for mime_type in parser.supported_mime_types
}


def parser_for_mime(mime_type: str | None) -> DocumentParser | None:
    if not mime_type:
        return None
    return PARSER_REGISTRY.get(mime_type)


def supported_mime_types() -> frozenset[str]:
    return frozenset(PARSER_REGISTRY)
