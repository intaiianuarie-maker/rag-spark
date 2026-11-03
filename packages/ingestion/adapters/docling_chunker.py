"""Docling HybridChunker adapter (T-OR-09, plan G6).

Integrates Docling's layout- and structure-aware HybridChunker into the
project-owned chunking interface. Operates on normalized `ParsedDocument` /
`DocumentElement` structures and outputs the same normalized `Chunk` /
`ChunkResult` models as the built-in and LlamaIndex chunkers.

Contract shared with ``ingestion.chunk`` (the other two backends):

* **Vision isolation.** Model-generated page descriptions
  (``metadata["extraction"] == "vision"``) never share a chunk with document
  text, and vision-only chunks carry ``metadata["extraction"] == "vision"``.
  OCR-bearing runs never cross a page boundary. Both rules are enforced by
  chunking each *run* of elements as its own ``DoclingDocument`` (see ``_runs``)
  — HybridChunker only ever merges peers inside one document.
* **Pages from sources.** Every item added to Docling is mapped back to its
  source ``DocumentElement`` by item ref, so a chunk's page / extraction /
  element types come from the elements it really contains, not from an index.
* **Tables** are real Docling ``TableItem``s so ``repeat_table_header`` works.

No docling vendor types leak beyond this module. Tokenization is deterministic
and offline: the tokenizer is always injected (no HuggingFace download, no
network at chunk time).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from docling_core.transforms.chunker.hierarchical_chunker import (
    ChunkingDocSerializer,
    ChunkingSerializerProvider,
)
from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer
from docling_core.transforms.serializer.markdown import MarkdownTableSerializer
from docling_core.types.doc import DocItemLabel, DoclingDocument, TableCell, TableData

from ..chunk import VISION_EXTRACTION, Chunk, ChunkResult
from ..interfaces import DocumentElement, ElementType, ParsedDocument
from ..metadata import DocumentMetadata, extract_metadata

logger = logging.getLogger("knowledge_os.ingestion.adapters.docling_chunker")

DEFAULT_MAX_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 0

BACKEND_NAME = "docling-hybrid-chunker"


class WhitespaceTokenizer(BaseTokenizer):
    """Deterministic, offline whitespace tokenizer."""

    max_tokens: int = DEFAULT_MAX_TOKENS

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def get_max_tokens(self) -> int:
        return self.max_tokens

    def get_tokenizer(self) -> Any:
        # semchunk (oversized plain text) needs a callable token counter.
        return self.count_tokens


class ApproxGeminiTokenizer(BaseTokenizer):
    """Deterministic offline approximation of Gemini token bounds.

    Uses conservative character/byte ratios (~4 chars per token) with safety
    headroom to prevent upstream truncation on real API embedding calls.
    """

    max_tokens: int = DEFAULT_MAX_TOKENS
    chars_per_token: float = 4.0

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        # Blend whitespace words and character length
        words = len(text.split())
        char_tokens = int(len(text) / self.chars_per_token)
        return max(words, char_tokens)

    def get_max_tokens(self) -> int:
        return self.max_tokens

    def get_tokenizer(self) -> Any:
        # semchunk (oversized plain text) needs a callable token counter.
        return self.count_tokens


class _MarkdownTableSerializerProvider(ChunkingSerializerProvider):
    """Markdown tables (like the parsers emit) instead of Docling's default triplets.

    ``repeat_table_header`` only repeats a real header row, which exists in the
    Markdown form; the triplet form has no header lines to repeat.
    """

    def get_serializer(self, doc: DoclingDocument) -> ChunkingDocSerializer:
        return ChunkingDocSerializer(doc=doc, table_serializer=MarkdownTableSerializer())


def _is_vision(element: DocumentElement) -> bool:
    return (element.metadata or {}).get("extraction") == VISION_EXTRACTION


def _runs(elements: list[DocumentElement]) -> list[list[DocumentElement]]:
    """Split the element stream where chunks must not span (``chunk._sections`` rules).

    A new run starts when vision/non-vision flips (model text never shares a
    chunk with document text) or when an OCR-bearing run would cross a page.
    Elements without text are dropped.
    """
    runs: list[list[DocumentElement]] = []
    current: list[DocumentElement] = []
    is_vision = has_ocr = False
    page: int | None = None
    for element in elements:
        if not (element.text or "").strip():
            continue
        el_ocr = bool(element.metadata and "extraction" in element.metadata)
        el_vision = _is_vision(element)
        if current and (el_vision != is_vision or ((el_ocr or has_ocr) and element.page != page)):
            runs.append(current)
            current = []
        if not current:
            is_vision, has_ocr, page = el_vision, False, element.page
        current.append(element)
        has_ocr = has_ocr or el_ocr
        if page is None:
            page = element.page
    if current:
        runs.append(current)
    return runs


def _table_rows(element: DocumentElement) -> tuple[list[list[str]], int]:
    """Rows + header row count of a TABLE element (structured table, else markdown)."""
    st = (element.metadata or {}).get("structured_table")
    if isinstance(st, dict) and st.get("rows"):
        return [[str(c) for c in r] for r in st["rows"]], int(st.get("header_rows", 1))
    rows: list[list[str]] = []
    for line in element.text.strip().splitlines():
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            return [], 0  # not a pipe table: caller falls back to plain text
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(set(c) <= set("-: ") and c for c in cells):
            continue  # markdown separator row
        rows.append(cells)
    return rows, 1


def _table_data(rows: list[list[str]], header_rows: int) -> TableData:
    num_cols = max(len(r) for r in rows)
    cells = [
        TableCell(
            text=r[c] if c < len(r) else "",
            start_row_offset_idx=i,
            end_row_offset_idx=i + 1,
            start_col_offset_idx=c,
            end_col_offset_idx=c + 1,
            column_header=i < header_rows,
        )
        for i, r in enumerate(rows)
        for c in range(num_cols)
    ]
    return TableData(num_rows=len(rows), num_cols=num_cols, table_cells=cells)


class DoclingHybridChunker:
    """Project adapter for Docling HybridChunker."""

    def __init__(
        self,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        tokenizer: str | BaseTokenizer | Callable[[str], int] = "whitespace",
        repeat_table_header: bool = True,
    ) -> None:
        self.max_tokens = max_tokens
        self.repeat_table_header = repeat_table_header

        if isinstance(tokenizer, BaseTokenizer):
            self.tokenizer = tokenizer
        elif tokenizer == "whitespace":
            self.tokenizer = WhitespaceTokenizer(max_tokens=max_tokens)
        elif tokenizer in ("gemini", "gemini_approx", "cl100k"):
            self.tokenizer = ApproxGeminiTokenizer(max_tokens=max_tokens)
        elif callable(tokenizer):
            token_counter: Callable[[str], int] = tokenizer

            class CallableTokenizer(BaseTokenizer):
                max_tokens: int = self.max_tokens
                fn: Callable[[str], int] = token_counter

                def count_tokens(self, text: str) -> int:
                    return self.fn(text)

                def get_max_tokens(self) -> int:
                    return self.max_tokens

                def get_tokenizer(self) -> Any:
                    return self.count_tokens

            self.tokenizer = CallableTokenizer()
        else:
            self.tokenizer = WhitespaceTokenizer(max_tokens=max_tokens)

        self._chunker = HybridChunker(
            tokenizer=self.tokenizer,
            repeat_table_header=self.repeat_table_header,
            serializer_provider=_MarkdownTableSerializerProvider(),
        )

    def chunk(
        self,
        parsed: ParsedDocument,
        *,
        filename: str | None = None,
        document_metadata: DocumentMetadata | None = None,
    ) -> ChunkResult:
        """Chunk a ParsedDocument into normalized Chunk models with provenance."""
        doc_meta = document_metadata or extract_metadata(parsed, filename=filename)
        doc_name = filename or doc_meta.filename or "document"

        chunks: list[Chunk] = []
        title: str | None = None
        heading: str | None = None
        for run in _runs(parsed.elements):
            vision = _is_vision(run[0])
            docling_doc = DoclingDocument(name=doc_name)
            by_ref: dict[str, DocumentElement] = {}

            # Re-state the enclosing heading context for a run that starts
            # mid-section (after a vision page / an OCR page break).
            if not vision:
                if title and run[0].type != ElementType.TITLE:
                    docling_doc.add_title(text=title)
                if heading and run[0].type not in (ElementType.TITLE, ElementType.HEADING):
                    docling_doc.add_heading(text=heading, level=1)

            for el in run:
                text = el.text.strip()
                if el.type == ElementType.TITLE:
                    title, heading = text, None
                    item = docling_doc.add_title(text=text)
                elif el.type == ElementType.HEADING:
                    heading = text
                    item = docling_doc.add_heading(text=text, level=1)
                else:
                    rows, header_rows = _table_rows(el) if el.type == ElementType.TABLE else ([], 0)
                    if rows:
                        item = docling_doc.add_table(data=_table_data(rows, header_rows))
                    else:
                        item = docling_doc.add_text(label=DocItemLabel.TEXT, text=text)
                by_ref[item.self_ref] = el

            run_chunks = 0
            parent_ids: dict[tuple[str, ...], str] = {}
            for d_chunk in self._chunker.chunk(docling_doc):
                # Contextualized text = enclosing headings + body, the form the
                # token cap is enforced on (builtin chunks also carry heading text).
                chunk_text = (self._chunker.contextualize(d_chunk) or "").strip()
                if not chunk_text:
                    continue
                headings = list(getattr(d_chunk.meta, "headings", None) or [])
                sources = [
                    by_ref[i.self_ref]
                    for i in getattr(d_chunk.meta, "doc_items", [])
                    if i.self_ref in by_ref
                ]
                chunks.append(
                    self._make(
                        len(chunks),
                        chunk_text,
                        sources=sources or run,
                        headings=headings,
                        parent_id=parent_ids.get(tuple(headings)),
                        doc_meta=doc_meta,
                    )
                )
                parent_ids.setdefault(tuple(headings), chunks[-1].id)
                run_chunks += 1

            if not run_chunks:
                # Heading-only run: Docling emits no chunk for bare headings, but
                # the built-in chunkers keep that text, so keep it here too.
                chunks.append(
                    self._make(
                        len(chunks),
                        " ".join(e.text.strip() for e in run),
                        sources=run,
                        headings=[],
                        parent_id=None,
                        doc_meta=doc_meta,
                    )
                )

        return ChunkResult(chunks=chunks, language=doc_meta.language, metadata=doc_meta)

    def _make(
        self,
        index: int,
        text: str,
        *,
        sources: list[DocumentElement],
        headings: list[str],
        parent_id: str | None,
        doc_meta: DocumentMetadata,
    ) -> Chunk:
        meta: dict[str, Any] = {
            "language": doc_meta.language,
            "chunking_backend": "docling_hybrid",
            "tokenizer": type(self.tokenizer).__name__,
        }
        if doc_meta.title:
            meta["title"] = doc_meta.title
        if doc_meta.filename:
            meta["filename"] = doc_meta.filename
        if headings:
            meta["headings"] = headings
        extraction = next(
            (e.metadata["extraction"] for e in sources if "extraction" in (e.metadata or {})),
            None,
        )
        if extraction is not None:
            meta["extraction"] = extraction
        table_prov = next(
            (
                e.metadata["structured_table"]
                for e in sources
                if e.type == ElementType.TABLE and (e.metadata or {}).get("structured_table")
            ),
            None,
        )
        if table_prov:
            meta["table_provenance"] = table_prov

        types = [e.type.value for e in sources]
        if headings:
            types.append(ElementType.HEADING.value)
        return Chunk(
            id=f"chunk-{index:04d}",
            index=index,
            text=text,
            token_count=self.tokenizer.count_tokens(text),
            parent_id=parent_id,
            section=sources[0].section or (headings[-1] if headings else None),
            page=next((e.page for e in sources if e.page is not None), None),
            element_types=list(dict.fromkeys(types)),
            metadata=meta,
        )


def chunk_document_hybrid(
    parsed: ParsedDocument,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    tokenizer: str | BaseTokenizer = "whitespace",
    filename: str | None = None,
    repeat_table_header: bool = True,
) -> ChunkResult:
    """Convenience functional interface for Docling HybridChunker."""
    chunker = DoclingHybridChunker(
        max_tokens=max_tokens,
        tokenizer=tokenizer,
        repeat_table_header=repeat_table_header,
    )
    return chunker.chunk(parsed, filename=filename)
