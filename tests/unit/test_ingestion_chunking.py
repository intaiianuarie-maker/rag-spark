"""Unit tests for the heading-aware chunker (M2-001b)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from ingestion.chunk import (
    chunk_document,
    chunk_elements,
    count_tokens,
    split_text_by_tokens,
)
from ingestion.interfaces import DocumentElement, ElementType, ParsedDocument

pytestmark = pytest.mark.unit


def _element(
    element_type: ElementType,
    text: str,
    *,
    section: str | None = None,
    page: int | None = None,
) -> DocumentElement:
    return DocumentElement(type=element_type, text=text, section=section, page=page)


def test_split_text_respects_the_token_cap() -> None:
    pieces = split_text_by_tokens(" ".join(f"w{i}" for i in range(10)), 4)
    assert [count_tokens(piece) for piece in pieces] == [4, 4, 2]


def test_headings_drive_boundaries_and_the_cap_is_respected() -> None:
    elements = [
        _element(ElementType.TITLE, "Doc Title"),
        _element(ElementType.HEADING, "1. Introduction", section="Introduction", page=1),
        _element(ElementType.TEXT, " ".join(["intro"] * 300), section="Introduction", page=1),
        _element(ElementType.HEADING, "2. Methods", section="Methods", page=2),
        _element(ElementType.TEXT, "short body", section="Methods", page=2),
    ]
    chunks = chunk_elements(elements, max_tokens=100)

    assert {chunk.section for chunk in chunks} >= {"Introduction", "Methods"}
    assert all(chunk.token_count <= 100 for chunk in chunks)

    intro = [chunk for chunk in chunks if chunk.section == "Introduction"]
    assert len(intro) > 1  # 300 words over a 100-token cap
    parent = intro[0]
    assert parent.parent_id is None
    children = [chunk for chunk in intro if chunk.parent_id is not None]
    assert children and all(child.parent_id == parent.id for child in children)

    methods = [chunk for chunk in chunks if chunk.section == "Methods"]
    assert len(methods) == 1
    assert methods[0].parent_id is None
    assert methods[0].page == 2


def test_parent_child_integrity_and_index_order() -> None:
    elements = [
        _element(ElementType.HEADING, "A", section="A"),
        _element(ElementType.TEXT, " ".join(["x"] * 500), section="A"),
    ]
    chunks = chunk_elements(elements, max_tokens=50)
    ids = {chunk.id for chunk in chunks}
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        if chunk.parent_id is not None:
            assert chunk.parent_id in ids
            assert chunk.parent_id != chunk.id


def test_chunking_is_deterministic() -> None:
    elements = [
        _element(ElementType.HEADING, "A", section="A"),
        _element(ElementType.TEXT, " ".join(["x"] * 300), section="A"),
    ]
    first = chunk_elements(elements, max_tokens=64)
    second = chunk_elements(elements, max_tokens=64)
    assert [chunk.model_dump() for chunk in first] == [chunk.model_dump() for chunk in second]


def test_chunk_document_attaches_language_and_metadata() -> None:
    parsed = ParsedDocument(
        elements=[
            _element(ElementType.TITLE, "Doc Title"),
            _element(ElementType.TEXT, " ".join(["word"] * 10)),
        ],
        mime_type="text/plain",
    )
    result = chunk_document(parsed, max_tokens=64, filename="doc.txt")
    assert result.language
    assert result.metadata.title == "Doc Title"
    assert result.chunks
    assert all(chunk.metadata["language"] == result.language for chunk in result.chunks)


def test_non_positive_max_tokens_is_rejected() -> None:
    with pytest.raises(ValueError):
        chunk_elements([_element(ElementType.TEXT, "hi")], max_tokens=0)
