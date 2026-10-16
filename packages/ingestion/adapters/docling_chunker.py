"""Docling HybridChunker adapter (T-OR-09).

Integrates Docling's layout- and structure-aware HybridChunker into the
project-owned chunking interface. Operates on normalized `ParsedDocument` /
`DocumentElement` structures and outputs normalized `Chunk` / `ChunkResult`
models with provenance, table header repetition, and token caps.

No docling vendor types leak beyond this module. Tokenization is deterministic
and offline: no runtime network calls or unapproved HuggingFace model downloads.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from docling.chunking import HybridChunker
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer
from docling_core.types.doc import DoclingDocument

from ..chunk import Chunk, ChunkResult
from ..interfaces import ElementType, ParsedDocument
from ..metadata import DocumentMetadata, extract_metadata

logger = logging.getLogger("knowledge_os.ingestion.adapters.docling_chunker")

DEFAULT_MAX_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 0


class WhitespaceTokenizer(BaseTokenizer):
    """Deterministic, offline whitespace tokenizer."""

    max_tokens: int = DEFAULT_MAX_TOKENS

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def get_max_tokens(self) -> int:
        return self.max_tokens

    def get_tokenizer(self) -> Any:
        return self


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
        return self


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
            class CallableTokenizer(BaseTokenizer):
                max_tokens: int = self.max_tokens
                fn: Callable[[str], int] = tokenizer

                def count_tokens(self, text: str) -> int:
                    return self.fn(text)

                def get_max_tokens(self) -> int:
                    return self.max_tokens

                def get_tokenizer(self) -> Any:
                    return self

            self.tokenizer = CallableTokenizer()
        else:
            self.tokenizer = WhitespaceTokenizer(max_tokens=max_tokens)

        self._chunker = HybridChunker(
            tokenizer=self.tokenizer,
            repeat_table_header=self.repeat_table_header,
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

        # Build internal DoclingDocument representation
        docling_doc = DoclingDocument(name=doc_name)
        page_map: list[int | None] = []
        section_map: list[str | None] = []
        table_meta_map: dict[int, dict[str, Any]] = {}

        current_section: str | None = None

        for el in parsed.elements:
            el_text = (el.text or "").strip()
            if not el_text:
                continue

            if el.section:
                current_section = el.section

            if el.type == ElementType.TITLE:
                current_section = el_text
                docling_doc.add_text(label="title", text=el_text)
                page_map.append(el.page)
                section_map.append(current_section)
            elif el.type == ElementType.HEADING:
                current_section = el_text
                docling_doc.add_text(label="section_header", text=el_text)
                page_map.append(el.page)
                section_map.append(current_section)
            elif el.type == ElementType.TABLE:
                st = el.metadata.get("structured_table") if el.metadata else None
                table_text = (st.get("markdown") if st and isinstance(st, dict) else "") or el_text
                docling_doc.add_text(label="text", text=table_text)
                t_idx = len(page_map)
                if st:
                    table_meta_map[t_idx] = st
                page_map.append(el.page)
                section_map.append(current_section)
            else:
                docling_doc.add_text(label="text", text=el_text)
                page_map.append(el.page)
                section_map.append(current_section)

        # Run HybridChunker
        doc_chunks = list(self._chunker.chunk(docling_doc))
        result_chunks: list[Chunk] = []

        for i, d_chunk in enumerate(doc_chunks):
            chunk_text = (d_chunk.text or "").strip()
            if not chunk_text:
                continue

            # Contextual headings from docling meta
            headings = getattr(d_chunk.meta, "headings", None) or []
            section = headings[-1] if headings else (current_section or None)

            # Determine page from item mapping or element provenance
            page: int | None = None
            if i < len(page_map) and page_map[i] is not None:
                page = page_map[i]
            elif parsed.elements:
                page = parsed.elements[0].page

            token_count = self.tokenizer.count_tokens(chunk_text)

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

            # Parent-child tracking
            parent_id: str | None = None
            if i > 0 and len(result_chunks) > 0:
                prev = result_chunks[-1]
                if prev.section == section and prev.page == page:
                    parent_id = prev.parent_id or prev.id

            elem_types = ["text"]
            if i in table_meta_map:
                meta["table_provenance"] = table_meta_map[i]
                elem_types.append("table")
            elif "|" in chunk_text and chunk_text.count("|") >= 2:
                elem_types.append("table")

            if any(h in chunk_text for h in headings):
                elem_types.append("heading")

            result_chunks.append(
                Chunk(
                    id=f"chunk-{len(result_chunks):04d}",
                    index=len(result_chunks),
                    text=chunk_text,
                    token_count=token_count,
                    parent_id=parent_id,
                    section=section,
                    page=page,
                    element_types=list(dict.fromkeys(elem_types)),
                    metadata=meta,
                )
            )

        return ChunkResult(
            chunks=result_chunks,
            language=doc_meta.language,
            metadata=doc_meta,
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
