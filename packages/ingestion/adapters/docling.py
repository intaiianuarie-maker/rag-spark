"""Docling parser adapter — docling (MIT, 2.134.0).

Wraps ``DocumentConverter`` (model-free ``NativePdfPipeline`` for PDFs to avoid
heavy ML deps) into our ``DocumentParser`` ABC. Output is normalized to
``ParsedDocument``/``DocumentElement`` with page/section/bbox preserved. No
docling types leak past this module.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from pydantic import BaseModel, Field

from ..interfaces import BBox, DocumentElement, DocumentParser, ElementType, ParsedDocument
from ..normalize import ParseBudgetExceeded, ParseError, clean_text, make_element, validate_input


class StructuredCell(BaseModel):
    """Normalized cell in a structured table."""

    text: str
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    is_header: bool = False
    bbox: BBox | None = None


class StructuredTable(BaseModel):
    """Project-owned structured table representation (T-OR-08)."""

    num_rows: int = 0
    num_cols: int = 0
    header_rows: int = 1
    markdown: str = ""
    rows: list[list[str]] = Field(default_factory=list)
    cells: list[StructuredCell] = Field(default_factory=list)
    page: int | None = None
    bbox: BBox | None = None
    profile: str = "native"

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

    def __init__(self, profile: str = "native", max_child_memory_mb: int = 2048) -> None:
        self.profile = profile
        self.max_child_memory_mb = max_child_memory_mb

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

        profile_limitation: str | None = None
        ext = _MIME_TO_EXT.get(mime_type, ".pdf")
        stream_name = filename or f"doc{ext}"
        if not stream_name.endswith(ext):
            stream_name = f"{stream_name}{ext}"

        format_options: dict[Any, Any] = {}
        if mime_type == "application/pdf":
            if self.profile in ("tableformer", "structured"):
                try:
                    from docling.datamodel.pipeline_options import PdfPipelineOptions
                    from docling.document_converter import PdfFormatOption

                    pipeline_options = PdfPipelineOptions()
                    pipeline_options.do_table_structure = True
                    format_options[InputFormat.PDF] = PdfFormatOption(
                        pipeline_options=pipeline_options
                    )
                except Exception as exc:
                    format_options[InputFormat.PDF] = NativePdfFormatOption(
                        pipeline_options=NativePdfPipelineOptions()
                    )
                    profile_limitation = (
                        f"Structured profile '{self.profile}' requested but unavailable ({exc}); "
                        "fell back to native profile."
                    )
            else:
                format_options[InputFormat.PDF] = NativePdfFormatOption(
                    pipeline_options=NativePdfPipelineOptions()
                )

        try:
            converter = DocumentConverter(format_options=format_options if format_options else None)
            result = converter.convert(DocumentStream(name=stream_name, stream=BytesIO(data)))
            doc = result.document
        except (ParseError, ParseBudgetExceeded):
            raise
        except Exception as exc:
            if self.profile in ("tableformer", "structured") and mime_type == "application/pdf":
                profile_limitation = (
                    f"Structured profile '{self.profile}' failed ({exc}); "
                    "fell back to native profile with disclosed limitations."
                )
                try:
                    native_opts = {
                        InputFormat.PDF: NativePdfFormatOption(
                            pipeline_options=NativePdfPipelineOptions()
                        )
                    }
                    converter = DocumentConverter(format_options=native_opts)
                    dstream = DocumentStream(name=stream_name, stream=BytesIO(data))
                    result = converter.convert(dstream)
                    doc = result.document
                except Exception as inner_exc:
                    err_msg = f"docling fallback conversion failed: {inner_exc}"
                    raise ParseError(err_msg) from inner_exc
            else:
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

                elements.append(make_element(el_type, text, page=page, section=section, bbox=bbox))

            for table in getattr(doc, "tables", []):
                try:
                    grid = getattr(getattr(table, "data", None), "grid", None)
                    prov = (getattr(table, "prov", None) or [None])[0]
                    page = getattr(prov, "page_no", None) if prov else None
                    bbox = _bbox_from_prov(prov) if prov else None

                    markdown_repr = ""
                    if hasattr(table, "export_to_markdown"):
                        try:
                            markdown_repr = str(table.export_to_markdown() or "").strip()
                        except Exception:
                            markdown_repr = ""

                    rows_text: list[list[str]] = []
                    cells: list[StructuredCell] = []
                    header_rows = 0

                    if grid:
                        for r_idx, row in enumerate(grid):
                            row_cells = []
                            row_is_header = False
                            for c_idx, cell in enumerate(row):
                                cell_text = clean_text(getattr(cell, "text", "") or "")
                                row_cells.append(cell_text)
                                is_hdr = bool(
                                    getattr(cell, "column_header", False)
                                    or getattr(cell, "row_header", False)
                                )
                                if is_hdr:
                                    row_is_header = True
                                c_bbox = _bbox_from_prov(cell) if hasattr(cell, "prov") else None
                                if (
                                    c_bbox is None
                                    and hasattr(cell, "bbox")
                                    and cell.bbox is not None
                                ):
                                    try:
                                        c_bbox = BBox(
                                            x0=float(cell.bbox.l),
                                            y0=float(cell.bbox.b),
                                            x1=float(cell.bbox.r),
                                            y1=float(cell.bbox.t),
                                        )
                                    except Exception:
                                        c_bbox = None

                                cells.append(
                                    StructuredCell(
                                        text=cell_text,
                                        row=getattr(cell, "start_row_offset_idx", r_idx),
                                        col=getattr(cell, "start_col_offset_idx", c_idx),
                                        row_span=getattr(cell, "row_span", 1),
                                        col_span=getattr(cell, "col_span", 1),
                                        is_header=is_hdr,
                                        bbox=c_bbox,
                                    )
                                )
                            if row_is_header:
                                header_rows += 1
                            rows_text.append(row_cells)

                    if not markdown_repr and rows_text:
                        header = " | ".join(rows_text[0])
                        separator = " | ".join("---" for _ in rows_text[0])
                        lines = [f"| {header} |", f"| {separator} |"]
                        for r in rows_text[1:]:
                            lines.append(f"| {' | '.join(r)} |")
                        markdown_repr = "\n".join(lines)

                    t_text = (
                        markdown_repr.strip()
                        if markdown_repr
                        else clean_text(getattr(table, "text", "") or "")
                    )
                    if not t_text and rows_text:
                        t_text = "\n".join(" | ".join(c for c in r if c) for r in rows_text)
                    if not t_text:
                        continue

                    num_rows = len(rows_text) if rows_text else 0
                    num_cols = max((len(r) for r in rows_text), default=0) if rows_text else 0

                    structured = StructuredTable(
                        num_rows=num_rows,
                        num_cols=num_cols,
                        header_rows=max(header_rows, 1 if num_rows > 1 else 0),
                        markdown=t_text,
                        rows=rows_text,
                        cells=cells,
                        page=page,
                        bbox=bbox,
                        profile=self.profile,
                    )

                    el = make_element(
                        ElementType.TABLE, t_text, page=page, section=section, bbox=bbox
                    )
                    el.metadata["structured_table"] = structured.model_dump()
                    el.metadata["profile"] = self.profile
                    elements.append(el)
                except Exception:
                    t_text = clean_text(getattr(table, "text", "") or "")
                    if t_text:
                        prov = (getattr(table, "prov", None) or [None])[0]
                        page = getattr(prov, "page_no", None) if prov else None
                        bbox = _bbox_from_prov(prov) if prov else None
                        elements.append(
                            make_element(
                                ElementType.TABLE, t_text, page=page, section=section, bbox=bbox
                            )
                        )
        except Exception as exc:
            raise ParseError(f"failed to normalize docling output: {exc}") from exc

        if not elements:
            raise ParseError("docling produced no extractable elements")

        combined = " ".join(e.text for e in elements)
        language = _detect_language(combined)

        out_meta = dict(metadata or {})
        if profile_limitation:
            out_meta["docling_profile_limitation"] = profile_limitation
        out_meta["docling_profile"] = self.profile

        return ParsedDocument(
            elements=elements,
            language=language,
            mime_type=mime_type,
            page_count=page_count,
            metadata=out_meta,
        )


PARSER = DoclingParser(profile="native")


def create_parser(profile: str = "native", max_child_memory_mb: int = 2048) -> DocumentParser:
    return DoclingParser(profile=profile, max_child_memory_mb=max_child_memory_mb)

