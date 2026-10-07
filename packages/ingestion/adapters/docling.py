"""Docling parser adapter — docling (MIT, 2.134.0).

Wraps ``DocumentConverter`` (model-free ``NativePdfPipeline`` for PDFs to avoid
heavy ML deps) into our ``DocumentParser`` ABC. Output is normalized to
``ParsedDocument``/``DocumentElement`` with page/section/bbox preserved. No
docling types leak past this module.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from ..interfaces import BBox, DocumentElement, DocumentParser, ElementType, ParsedDocument
from ..normalize import ParseError, clean_text, make_element, validate_input

SUPPORTED_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        "text/html",
        "text/markdown",
        "text/plain",
    }
)

_MIME_TO_EXT = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml": ".docx",
    "text/html": ".html",
    "text/markdown": ".md",
    "text/plain": ".txt",
}


def _detect_language(text: str) -> str | None:
    sample = text[:2000]
    if "ă" in sample or "ș" in sample or "ț" in sample or "Bună" in sample or "Secțiunea" in sample:
        return "ro"
    if "ã" in sample or "õ" in sample or "ç" in sample or "Olá" in sample or "Secção" in sample:
        return "pt"
    if sample.strip():
        return "en"
    return None


def _bbox_from_prov(prov: Any) -> BBox | None:
    try:
        bbox = prov.bbox
        if bbox is None:
            return None
        # docling bbox is BOTTOMLEFT origin: l,b,r,t
        return BBox(x0=float(bbox.l), y0=float(bbox.b), x1=float(bbox.r), y1=float(bbox.t))
    except Exception:
        return None


class DoclingParser(DocumentParser):
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
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import NativePdfPipelineOptions
            from docling.document_converter import DocumentConverter, NativePdfFormatOption
            from docling_core.types.io import DocumentStream
        except Exception as exc:
            raise ParseError(f"docling not available: {exc}") from exc

        try:
            ext = _MIME_TO_EXT.get(mime_type, ".pdf")
            stream_name = filename or f"doc{ext}"
            if not stream_name.endswith(ext):
                stream_name = f"{stream_name}{ext}"

            # Use NativePdfPipeline for PDFs to avoid heavy layout/table models.
            format_options: dict[Any, Any] = {}
            if mime_type == "application/pdf":
                format_options[InputFormat.PDF] = NativePdfFormatOption(
                    pipeline_options=NativePdfPipelineOptions()
                )

            converter = DocumentConverter(
                format_options=format_options if format_options else None
            )
            result = converter.convert(DocumentStream(name=stream_name, stream=BytesIO(data)))
            doc = result.document
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"docling conversion failed: {exc}") from exc

        elements: list[DocumentElement] = []
        section: str | None = None
        page_count: int | None = None

        try:
            # Determine page count from texts prov if available
            pages = set()
            for item in getattr(doc, "texts", []):
                for prov in getattr(item, "prov", []) or []:
                    if hasattr(prov, "page_no"):
                        pages.add(prov.page_no)
            if pages:
                page_count = max(pages)

            for item in getattr(doc, "texts", []):
                text = clean_text(getattr(item, "text", "") or "")
                if not text:
                    continue
                prov = (getattr(item, "prov", None) or [None])[0]
                page = getattr(prov, "page_no", None) if prov else None
                bbox = _bbox_from_prov(prov) if prov else None
                label = str(getattr(item, "label", "") or "").lower()

                # Derive element type and section tracking
                if label in {"title", "document_title"}:
                    el_type = ElementType.TITLE
                    section = text
                elif label in {"section_header", "header", "heading"} or text.startswith("#"):
                    el_type = ElementType.HEADING
                    section = text
                else:
                    # Fallback: numbered heading heuristic
                    if len(text) < 80 and text.startswith(("1.", "2.", "3.")):
                        el_type = ElementType.HEADING
                        section = text
                    else:
                        el_type = ElementType.TEXT

                elements.append(
                    make_element(el_type, text, page=page, section=section, bbox=bbox)
                )

            for table in getattr(doc, "tables", []):
                # Tables: concatenate cell texts if available, else fallback to table text
                try:
                    grid = getattr(getattr(table, "data", None), "grid", None)
                    if grid:
                        rows = []
                        for row in grid:
                            cells = [clean_text(getattr(cell, "text", "") or "") for cell in row]
                            rows.append(" | ".join(c for c in cells if c))
                        t_text = "\n".join(r for r in rows if r)
                    else:
                        t_text = clean_text(getattr(table, "text", "") or "")
                except Exception:
                    t_text = clean_text(getattr(table, "text", "") or "")
                if not t_text:
                    continue
                prov = (getattr(table, "prov", None) or [None])[0]
                page = getattr(prov, "page_no", None) if prov else None
                bbox = _bbox_from_prov(prov) if prov else None
                elements.append(
                    make_element(ElementType.TABLE, t_text, page=page, section=section, bbox=bbox)
                )
        except Exception as exc:
            raise ParseError(f"failed to normalize docling output: {exc}") from exc

        if not elements:
            raise ParseError("docling produced no extractable elements")

        combined = " ".join(e.text for e in elements)
        language = _detect_language(combined)

        return ParsedDocument(
            elements=elements,
            language=language,
            mime_type=mime_type,
            page_count=page_count,
            metadata=dict(metadata or {}),
        )


PARSER = DoclingParser()


def create_parser() -> DocumentParser:
    return DoclingParser()
