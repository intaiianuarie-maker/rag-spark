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


class PageClassification(StrEnum):
    """Classification of a document page for routing (native vs OCR)."""

    NATIVE_TEXT = "native_text"
    OCR_REQUIRED = "ocr_required"
    BLANK = "blank"
    UNCERTAIN = "uncertain"


class OcrWord(BaseModel):
    """A single recognized word with bounding box and confidence score."""

    text: str
    confidence: float
    bbox: BBox
    line_num: int
    block_num: int
    par_num: int = 0
    word_num: int = 0


class OcrPageRequest(BaseModel):
    """Request for performing OCR on a single rendered page."""

    page_number: int
    dpi: int = 300
    languages: str = "ron+eng+por"
    psm: int = 3
    timeout_seconds: float = 60.0


class OcrPageResult(BaseModel):
    """OCR output for a single page, containing extracted words and elements."""

    page_number: int
    text: str
    words: list[OcrWord] = Field(default_factory=list)
    mean_confidence: float = 0.0
    width_pts: float
    height_pts: float
    language: str | None = None
    elements: list[DocumentElement] = Field(default_factory=list)
    extraction_method: str = "tesseract"
    engine_version: str | None = None


class ExtractionReport(BaseModel):
    """Structured extraction report recording native and OCR coverage."""

    native_pages: list[int] = Field(default_factory=list)
    ocr_pages: list[int] = Field(default_factory=list)
    blank_pages: list[int] = Field(default_factory=list)
    uncertain_pages: list[int] = Field(default_factory=list)
    failed_pages: list[int] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    engine: str = "tesseract"
    engine_version: str | None = None
    profile_hash: str | None = None
    total_pages: int = 0
    elapsed_seconds: float = 0.0

    @property
    def has_ocr(self) -> bool:
        return bool(self.ocr_pages)

    @property
    def fully_lost(self) -> bool:
        return (
            bool(self.total_pages)
            and not self.native_pages
            and not self.ocr_pages
            and bool(self.failed_pages)
        )

    @property
    def partially_lost(self) -> bool:
        return bool(self.failed_pages or self.uncertain_pages) and bool(
            self.native_pages or self.ocr_pages
        )

    def as_metadata(self) -> dict[str, Any]:
        return {
            "native_pages": self.native_pages,
            "ocr_pages": self.ocr_pages,
            "blank_pages": self.blank_pages,
            "uncertain_pages": self.uncertain_pages,
            "failed_pages": self.failed_pages,
            "languages": self.languages,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "profile_hash": self.profile_hash,
            "total_pages": self.total_pages,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }
