"""CSV adapter — standard library ``csv`` + charset-normalizer (PD-001).

The header row is a column definition, not data; each data row becomes one
TABLE element carrying the row's ``language`` tag (the citation-relevant
metadata for a flat multilingual source). Quoted commas are preserved.
"""

from __future__ import annotations

import csv
from io import StringIO
from typing import Any

from ..interfaces import DocumentElement, DocumentParser, ElementType, ParsedDocument
from ..normalize import ParseBudgetExceeded, ParseError, decode_text, make_element, validate_input

SUPPORTED_MIME_TYPES = frozenset({"text/csv", "application/csv"})


class CsvParser(DocumentParser):
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
            text = decode_text(data, (metadata or {}).get("charset"))
            reader = csv.DictReader(StringIO(text))
            elements = _row_elements(reader)
        except (ParseError, ParseBudgetExceeded):
            raise
        except csv.Error as exc:
            raise ParseError(f"invalid CSV: {exc}") from exc
        except Exception as exc:
            raise ParseError(f"failed to parse CSV: {exc}") from exc

        if not elements:
            raise ParseError("CSV produced no data rows")
        return ParsedDocument(elements=elements, mime_type=mime_type, metadata=dict(metadata or {}))


def _row_elements(reader: csv.DictReader) -> list[DocumentElement]:
    elements: list[DocumentElement] = []
    for index, row in enumerate(reader, start=1):
        row_text = " | ".join(f"{key}: {value}" for key, value in row.items() if value)
        if not row_text:
            continue
        row_metadata: dict[str, Any] = {"row": index}
        language = (row.get("lang") or "").strip().lower()
        if language:
            row_metadata["language"] = language
        elements.append(make_element(ElementType.TABLE, row_text, metadata=row_metadata))
    return elements


PARSER = CsvParser()


def create_parser() -> DocumentParser:
    return CsvParser()
