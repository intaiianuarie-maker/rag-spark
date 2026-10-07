"""Shared normalization helpers + typed ingestion errors (M2-001a).

Parsers emit our own :class:`ingestion.interfaces.DocumentElement` values; no
parser-specific object may cross this boundary (`docs/architecture/module-
boundaries.md`). This module holds only format-agnostic glue (input validation,
heading/section heuristics, element construction).
"""

from __future__ import annotations

import re
from typing import Any

from charset_normalizer import from_bytes

from .interfaces import BBox, DocumentElement, ElementType

#: Refuse documents above this size before parsing (task: oversized → typed error).
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024

_NUMBERED_HEADING = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+\S")
_NUMBERING_PREFIX = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+")


class IngestionError(Exception):
    """Base class for typed ingestion failures (never a raw parser exception)."""

    code = "ingestion_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnsupportedMimeTypeError(IngestionError):
    code = "unsupported_mime_type"


class EmptyDocumentError(IngestionError):
    code = "empty_document"


class DocumentTooLargeError(IngestionError):
    code = "document_too_large"


class ParseError(IngestionError):
    code = "parse_error"


def validate_input(
    data: bytes,
    mime_type: str,
    supported_mime_types: frozenset[str],
    *,
    max_bytes: int = MAX_DOCUMENT_BYTES,
) -> None:
    """Validate size/mime before handing bytes to a third-party parser."""
    if mime_type not in supported_mime_types:
        raise UnsupportedMimeTypeError(f"unsupported mime type: {mime_type}")
    if not data:
        raise EmptyDocumentError("empty document")
    if len(data) > max_bytes:
        raise DocumentTooLargeError(f"document exceeds {max_bytes} bytes")


def is_numbered_heading(text: str) -> bool:
    """True for numbered headings like ``1. Introduction`` / ``2.1 Methods``."""
    return bool(_NUMBERED_HEADING.match(text))


def strip_numbering(text: str) -> str:
    """Remove a leading numbering prefix, yielding the section name."""
    return _NUMBERING_PREFIX.sub("", text).strip()


def clean_text(text: str) -> str:
    return " ".join(text.split())


def decode_text(data: bytes) -> str:
    """Decode text bytes as UTF-8, falling back to charset-normalizer detection."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        best = from_bytes(data).best()
        if best is not None:
            return str(best)
        return data.decode("utf-8", errors="replace")


def make_element(
    element_type: ElementType,
    text: str,
    *,
    page: int | None = None,
    section: str | None = None,
    bbox: BBox | None = None,
    metadata: dict[str, Any] | None = None,
) -> DocumentElement:
    return DocumentElement(
        type=element_type,
        text=text,
        page=page,
        section=section,
        bbox=bbox,
        metadata=metadata or {},
    )
