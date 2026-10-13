"""PDF text-layer report — honest "what could we not read" detection (T-V1-031).

A scanned PDF is a PDF that opens fine and extracts *nothing*: its pages are
images with no text layer. Under the old path that surfaced either as a hard
parse failure (fully scanned) or as a READY document whose every page silently
came back empty (partially scanned) — in both cases the user was never told
which regions were lost, and an answer over such a source could look
confident.

This module answers one question before any parse runs: **which pages carry an
image and no text?** Those are the lost regions. It is a text-layer inspection
only (pypdf, a PD-001 dependency) — no OCR, no model, no network. OCR is not
part of this deployment, and the honest answer to "we cannot read this page"
is to say so, not to guess at it.

The distinction the report draws:

* page with a text layer  ->  read normally;
* page with an image **and** no text  ->  **lost region**, disclosed;
* page with neither  ->  blank; nothing was lost, so nothing is disclosed.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any

#: Reason code recorded on the disclosure for every lost page.
REASON_NO_TEXT_LAYER = "no_text_layer"


@dataclass(frozen=True)
class PdfTextLayerReport:
    """What the text layer of one PDF does and does not cover."""

    page_count: int
    pages_with_text: tuple[int, ...]
    #: Pages carrying an image XObject and no text — the lost regions.
    lost_pages: tuple[int, ...]

    @property
    def fully_lost(self) -> bool:
        """True when the document is image-only (nothing at all is readable)."""
        return bool(self.page_count) and not self.pages_with_text and bool(self.lost_pages)

    @property
    def partially_lost(self) -> bool:
        """True when some pages are readable and others are lost."""
        return bool(self.pages_with_text) and bool(self.lost_pages)

    def disclosure(self) -> dict[str, Any]:
        """The disclosure the pipeline stores on the job and its chunks."""
        return {
            "reason": REASON_NO_TEXT_LAYER,
            "lost_pages": list(self.lost_pages),
            "page_count": self.page_count,
            "readable_pages": len(self.pages_with_text),
        }


def _page_has_image(page: Any) -> bool:
    try:
        resources = page.get("/Resources")
        resources = resources.get_object() if resources is not None else None
        xobjects = resources.get("/XObject") if resources else None
        xobjects = xobjects.get_object() if xobjects is not None else None
        if not xobjects:
            return False
        return any(
            getattr(obj.get_object(), "get", lambda _key, _default=None: None)("/Subtype")
            == "/Image"
            for obj in xobjects.values()
        )
    except Exception:  # unreadable resources are not proof of an image
        return False


def pdf_text_layer_report(data: bytes) -> PdfTextLayerReport | None:
    """Inspect a PDF's text layer; ``None`` when the bytes are not a readable PDF.

    ``None`` means "could not inspect", never "no text layer" — the caller
    treats an unreadable file as a parse failure, not as a disclosure.
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        pages_with_text: list[int] = []
        lost_pages: list[int] = []
        page_count = 0
        for page_number, page in enumerate(reader.pages, start=1):
            page_count = page_number
            has_text = bool((page.extract_text() or "").strip())
            has_image = _page_has_image(page)
            if has_text:
                pages_with_text.append(page_number)
            elif has_image:
                lost_pages.append(page_number)
    except Exception:
        return None
    return PdfTextLayerReport(
        page_count=page_count,
        pages_with_text=tuple(pages_with_text),
        lost_pages=tuple(lost_pages),
    )
