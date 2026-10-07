"""Plain-text adapter — stdlib + charset-normalizer (PD-001).

Paragraphs (blank-line separated) become TEXT elements; encoding is detected so
latin/utf-8 files both decode. Page/section are not applicable to TXT.
"""

from __future__ import annotations

import re
from typing import Any

from ..interfaces import DocumentElement, DocumentParser, ElementType, ParsedDocument
from ..normalize import ParseError, clean_text, decode_text, make_element, validate_input

SUPPORTED_MIME_TYPES = frozenset({"text/plain"})
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


class TextParser(DocumentParser):
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
            text = decode_text(data)
            elements = _paragraph_elements(text)
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"failed to parse text: {exc}") from exc

        if not elements:
            raise ParseError("text produced no extractable content")
        return ParsedDocument(elements=elements, mime_type=mime_type, metadata=dict(metadata or {}))


def _paragraph_elements(text: str) -> list[DocumentElement]:
    elements: list[DocumentElement] = []
    for block in _PARAGRAPH_SPLIT.split(text):
        paragraph = clean_text(block)
        if paragraph:
            elements.append(make_element(ElementType.TEXT, paragraph))
    return elements


PARSER = TextParser()


def create_parser() -> DocumentParser:
    return TextParser()
