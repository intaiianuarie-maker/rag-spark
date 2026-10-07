"""PDF table/structure adapter — pdfplumber (PD-001, MIT).

Keeps ruled tables as ``TABLE`` elements (cells joined, never flattened into a
single text blob) and extracts larger-font lines as headings. Body text that is
not inside a table is kept as ``TEXT``.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

import pdfplumber

from ..interfaces import DocumentElement, DocumentParser, ElementType, ParsedDocument
from ..normalize import ParseError, clean_text, make_element, validate_input

SUPPORTED_MIME_TYPES = frozenset({"application/pdf"})
_HEADING_MIN_SIZE = 13.0


class PdfTableParser(DocumentParser):
    supported_mime_types = SUPPORTED_MIME_TYPES

    async def parse(
        self,
        data: bytes,
        *,
        mime_type: str,
        filename: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ParsedDocument:
        validate_input(data, mime_type, SUPPORTED_MIME_TYPES)
        elements: list[DocumentElement] = []
        try:
            with pdfplumber.open(BytesIO(data)) as pdf:
                for page_number, page in enumerate(pdf.pages, start=1):
                    elements.extend(_page_elements(page, page_number))
                page_count = len(pdf.pages)
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"failed to parse PDF structure: {exc}") from exc

        if not elements:
            raise ParseError("PDF produced no extractable structure")
        return ParsedDocument(
            elements=elements,
            mime_type=mime_type,
            page_count=page_count,
            metadata=dict(metadata or {}),
        )


def _page_elements(page: Any, page_number: int) -> list[DocumentElement]:
    elements: list[DocumentElement] = []
    section: str | None = None

    table_bboxes = [table.bbox for table in page.find_tables()]
    for line in page.extract_text_lines() or []:
        text = clean_text(line.get("text", ""))
        if not text or _inside_table(line.get("top"), line.get("bottom"), table_bboxes):
            continue
        size = max((char.get("size", 0.0) for char in line.get("chars", [])), default=0.0)
        if size >= _HEADING_MIN_SIZE:
            section = text
            elements.append(
                make_element(ElementType.HEADING, text, page=page_number, section=section)
            )
        else:
            elements.append(
                make_element(ElementType.TEXT, text, page=page_number, section=section)
            )

    for table in page.extract_tables() or []:
        table_text = _table_text(table)
        if table_text:
            elements.append(
                make_element(ElementType.TABLE, table_text, page=page_number, section=section)
            )
    return elements


def _inside_table(top: float | None, bottom: float | None, bboxes: list[tuple]) -> bool:
    if top is None or bottom is None:
        return False
    for bbox in bboxes:
        _x0, table_top, _x1, table_bottom = bbox
        if top >= table_top - 1 and bottom <= table_bottom + 1:
            return True
    return False


def _table_text(table: list[list[str | None]]) -> str:
    rows = [" ".join(cell for cell in row if cell) for row in table if row]
    return " | ".join(row for row in rows if row)


PARSER = PdfTableParser()


def create_parser() -> DocumentParser:
    return PdfTableParser()
