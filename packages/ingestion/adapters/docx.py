"""DOCX adapter — python-docx (PD-001, MIT).

Preserves body order (paragraphs + tables), maps Heading 1 -> TITLE and lower
headings -> HEADING (section), keeps tables as TABLE with their cells.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from ..interfaces import DocumentElement, DocumentParser, ElementType, ParsedDocument
from ..normalize import ParseError, clean_text, make_element, validate_input

SUPPORTED_MIME_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)


class DocxParser(DocumentParser):
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
        try:
            document = Document(BytesIO(data))
            elements = _body_elements(document)
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"failed to parse DOCX: {exc}") from exc

        if not elements:
            raise ParseError("DOCX produced no extractable content")
        return ParsedDocument(elements=elements, mime_type=mime_type, metadata=dict(metadata or {}))


def _body_elements(document: Any) -> list[DocumentElement]:
    elements: list[DocumentElement] = []
    section: str | None = None
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = clean_text(paragraph.text)
            if not text:
                continue
            style = paragraph.style.name if paragraph.style is not None else ""
            if style == "Title" or style.startswith("Heading 1"):
                elements.append(make_element(ElementType.TITLE, text))
            elif style.startswith("Heading"):
                section = text
                elements.append(make_element(ElementType.HEADING, text, section=section))
            else:
                elements.append(make_element(ElementType.TEXT, text, section=section))
        elif tag == "tbl":
            table = Table(child, document)
            rows = [[cell.text for cell in row.cells] for row in table.rows]
            table_text = " | ".join(" ".join(cell for cell in row if cell) for row in rows if row)
            if table_text:
                elements.append(make_element(ElementType.TABLE, table_text, section=section))
    return elements


PARSER = DocxParser()


def create_parser() -> DocumentParser:
    return DocxParser()
