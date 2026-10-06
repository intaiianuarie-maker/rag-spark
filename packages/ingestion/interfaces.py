"""Document parsing contracts (decisions/PD-001-v1-parser-stack.md).

Boundary rule (`docs/architecture/module-boundaries.md`): third-party parser
objects must never cross this interface. Parsers return our normalized
:class:`ParsedDocument` / :class:`DocumentElement` tree, preserving
page/section/bbox for citations.

Future adapters (NOT wired in M0, no dependency added yet):

- PDF text: ``pypdf`` (BSD-3-Clause) — primary text/page-map extractor
- PDF tables/structure: ``pdfplumber`` (MIT) — fallback for tables/bbox
- DOCX: ``python-docx`` (MIT)
- HTML: ``beautifulsoup4`` + ``lxml`` (MIT)
- Markdown: ``markdown-it-py`` (MIT)
- TXT/CSV: Python stdlib + ``charset-normalizer`` (PSFL/MIT)
- Language tagging (RO/PT/EN): ``lingua``/``fast-langdetect`` (Apache-2.0)
- Optional, isolated: LlamaIndex ``readers-file`` (MIT) behind this interface

RAGFlow (Apache-2.0) contributes the *pattern* (normalize first, never flatten
early); no code is copied.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field


class ElementType(StrEnum):
    """Normalized element kinds produced by parsers."""

    TITLE = "title"
    HEADING = "heading"
    TEXT = "text"
    LIST = "list"
    TABLE = "table"
    CODE = "code"
    IMAGE = "image"
    FORMULA = "formula"
    HEADER = "header"
    FOOTER = "footer"
    OTHER = "other"


class BBox(BaseModel):
    """Optional bounding box (PDF/HTML coordinates) for source highlighting."""

    x0: float
    y0: float
    x1: float
    y1: float


class DocumentElement(BaseModel):
    """One normalized unit of parsed source content.

    Fields mirror `docs/rag/ingestion-spec.md`: type, text, page, section,
    bbox, metadata.
    """

    type: ElementType
    text: str
    page: int | None = None
    section: str | None = None
    bbox: BBox | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParsedDocument(BaseModel):
    """Result of parsing a single source version."""

    elements: list[DocumentElement] = Field(default_factory=list)
    language: str | None = None
    mime_type: str | None = None
    page_count: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentParser(ABC):
    """Adapter boundary isolating third-party parsers (PD-001).

    Implementations must live under an ingestion ``adapters/`` package and may
    not leak parser-specific types (pypdf/pdfplumber/docx/bs4/LlamaIndex) to
    callers.
    """

    #: MIME types this parser accepts (e.g. ``{"application/pdf"}``).
    supported_mime_types: ClassVar[frozenset[str]] = frozenset()

    @abstractmethod
    async def parse(
        self,
        data: bytes,
        *,
        mime_type: str,
        filename: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ParsedDocument:
        """Parse raw bytes into normalized elements.

        Must preserve page and section boundaries where the source provides
        them, and tag ``ParsedDocument.language`` for RO/PT/EN retrieval.
        """
        raise NotImplementedError
