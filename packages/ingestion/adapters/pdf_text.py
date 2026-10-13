"""PDF text adapter — pypdf (PD-001, BSD-3-Clause).

Extracts per-page text, preserving page numbers and tracking sections from
numbered headings (``1. Introduction``). Tables are handled by the separate
pdfplumber adapter (`pdf_table.py`), per PD-001.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from pypdf import PdfReader

from ..interfaces import DocumentElement, DocumentParser, ElementType, ParsedDocument
from ..normalize import (
    ParseBudgetExceeded,
    ParseError,
    clean_text,
    is_numbered_heading,
    make_element,
    strip_numbering,
    validate_input,
)

SUPPORTED_MIME_TYPES = frozenset({"application/pdf"})


class PdfTextParser(DocumentParser):
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
            reader = PdfReader(BytesIO(data))
            elements: list[DocumentElement] = []
            #: T-V1-031: pages the text layer could not yield anything from.
            #: A page that carries an image and no text is a *lost region*, and
            #: the pipeline discloses it (``needs_review``) instead of pretending
            #: the document was read in full. A genuinely blank page is not
            #: reported: nothing was lost from it.
            pages_without_text: list[int] = []
            for page_number, page in enumerate(reader.pages, start=1):
                page_text = page.extract_text() or ""
                if not page_text.strip():
                    pages_without_text.append(page_number)
                    continue
                elements.extend(_page_elements(page_text, page_number))
            page_count = len(reader.pages)
        except (ParseError, ParseBudgetExceeded):
            raise
        except Exception as exc:  # third-party failure -> typed error
            raise ParseError(f"failed to parse PDF text: {exc}") from exc

        if not elements:
            raise ParseError("PDF produced no extractable text")
        return ParsedDocument(
            elements=elements,
            mime_type=mime_type,
            page_count=page_count,
            metadata={
                **dict(metadata or {}),
                "pages_without_text": pages_without_text,
            },
        )


def _page_elements(text: str, page_number: int) -> list[DocumentElement]:
    elements: list[DocumentElement] = []
    section: str | None = None
    first_line = True
    for raw in text.splitlines():
        line = clean_text(raw)
        if not line:
            continue
        if page_number == 1 and first_line:
            first_line = False
            elements.append(make_element(ElementType.TITLE, line, page=page_number))
            continue
        first_line = False
        if is_numbered_heading(line):
            section = strip_numbering(line)
            elements.append(
                make_element(ElementType.HEADING, line, page=page_number, section=section)
            )
            continue
        elements.append(make_element(ElementType.TEXT, line, page=page_number, section=section))
    return elements


PARSER = PdfTextParser()


def create_parser() -> DocumentParser:
    return PdfTextParser()
