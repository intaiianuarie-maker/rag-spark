"""Heading-aware, token-capped chunker with parent-child links (M2-001b).

Deterministic (no embeddings/LLM, stable ids). Section boundaries come from
``DocumentElement.section`` and TITLE/HEADING elements. A section that fits in
``max_tokens`` becomes one self-contained chunk; a larger section becomes a
parent (lead) chunk plus continuation children whose ``parent_id`` links back to
it. Token counting is whitespace-based (documented approximation; a subword
tokenizer can be swapped behind ``count_tokens``/``split_text_by_tokens``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .interfaces import DocumentElement, ElementType, ParsedDocument
from .metadata import DocumentMetadata, extract_metadata

DEFAULT_MAX_TOKENS = 256


class Chunk(BaseModel):
    id: str
    index: int
    text: str
    token_count: int
    parent_id: str | None = None
    section: str | None = None
    page: int | None = None
    element_types: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChunkResult(BaseModel):
    chunks: list[Chunk]
    language: str
    metadata: DocumentMetadata


def count_tokens(text: str) -> int:
    """Deterministic token count (whitespace words)."""
    return len(text.split())


def split_text_by_tokens(text: str, max_tokens: int) -> list[str]:
    """Split ``text`` into pieces of at most ``max_tokens`` whitespace words."""
    words = text.split()
    if not words:
        return []
    return [
        " ".join(words[start : start + max_tokens])
        for start in range(0, len(words), max_tokens)
    ]


def _sections(elements: list[DocumentElement]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for element in elements:
        if current is None:
            new_section = True
        elif element.type in {ElementType.TITLE, ElementType.HEADING}:
            new_section = True
        elif element.section is not None and element.section != current["section"]:
            new_section = True
        else:
            new_section = False

        if new_section:
            current = {
                "section": element.section,
                "texts": [],
                "types": [],
                "page": element.page,
            }
            groups.append(current)
        if current is None:  # unreachable; keeps the type checker honest
            continue
        current["texts"].append(element.text)
        current["types"].append(element.type.value)
        if current["page"] is None and element.page is not None:
            current["page"] = element.page
    return groups


def chunk_elements(
    elements: list[DocumentElement],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    language: str = "unknown",
    document_metadata: DocumentMetadata | None = None,
) -> list[Chunk]:
    """Chunk a normalized element stream, honoring headings and the token cap."""
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")

    chunks: list[Chunk] = []
    for group in _sections(elements):
        combined = " ".join(text for text in group["texts"] if text).strip()
        if not combined:
            continue
        pieces = split_text_by_tokens(combined, max_tokens)
        if len(pieces) <= 1:
            chunks.append(
                _make_chunk(
                    len(chunks),
                    pieces[0] if pieces else "",
                    parent_id=None,
                    group=group,
                    language=language,
                    document_metadata=document_metadata,
                )
            )
            continue

        parent_id = f"chunk-{len(chunks):04d}"
        chunks.append(
            _make_chunk(
                len(chunks),
                pieces[0],
                parent_id=None,
                group=group,
                language=language,
                document_metadata=document_metadata,
            )
        )
        for piece in pieces[1:]:
            chunks.append(
                _make_chunk(
                    len(chunks),
                    piece,
                    parent_id=parent_id,
                    group=group,
                    language=language,
                    document_metadata=document_metadata,
                )
            )
    return chunks


def chunk_document(
    parsed: ParsedDocument,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    filename: str | None = None,
) -> ChunkResult:
    """Chunk a parsed document and attach its metadata + language tag."""
    metadata = extract_metadata(parsed, filename=filename)
    chunks = chunk_elements(
        parsed.elements,
        max_tokens=max_tokens,
        language=metadata.language,
        document_metadata=metadata,
    )
    return ChunkResult(chunks=chunks, language=metadata.language, metadata=metadata)


def _make_chunk(
    index: int,
    text: str,
    *,
    parent_id: str | None,
    group: dict[str, Any],
    language: str,
    document_metadata: DocumentMetadata | None,
) -> Chunk:
    metadata: dict[str, Any] = {"language": language}
    if document_metadata is not None:
        if document_metadata.title:
            metadata["title"] = document_metadata.title
        if document_metadata.filename:
            metadata["filename"] = document_metadata.filename
    return Chunk(
        id=f"chunk-{index:04d}",
        index=index,
        text=text,
        token_count=count_tokens(text),
        parent_id=parent_id,
        section=group["section"],
        page=group["page"],
        element_types=list(dict.fromkeys(group["types"])),
        metadata=metadata,
    )


# --- public seams for the LlamaIndex adapter (rag-fork-solution TASK-M2-002) ---
# Additive aliases only: the built-in chunker above keeps its exact behavior;
# the adapter reuses our heading-aware sectioning and chunk assembly so both
# backends emit the identical Chunk contract.

sections_from_elements = _sections
make_chunk = _make_chunk
