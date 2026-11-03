"""Docling HybridChunker adapter: vision isolation, source-derived pages, tables (plan G6).

Real docling-core (``docling-core[chunking]``) — no mocks. Mirrors the
guarantees of ``test_vision_provenance.py`` / ``test_ingestion_chunking.py`` for
the builtin chunker so all backends honour one contract.
"""

from __future__ import annotations

import os
import socket

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytest.importorskip("docling_core.transforms.chunker.hybrid_chunker")

from ingestion.adapters.docling_chunker import chunk_document_hybrid
from ingestion.interfaces import DocumentElement, ElementType, ParsedDocument

pytestmark = pytest.mark.unit

VISION = "[Model-generated page description, page 1] A bar chart of sales."


def _el(text, *, page=1, type=ElementType.TEXT, section=None, **meta):
    return DocumentElement(type=type, text=text, page=page, section=section, metadata=meta)


def _chunk(elements, max_tokens=50):
    return chunk_document_hybrid(
        ParsedDocument(elements=elements, language="en"), max_tokens=max_tokens
    )


def test_vision_never_shares_a_chunk_with_document_text():
    res = _chunk(
        [
            _el("Plain text before the figure."),
            _el(VISION, extraction="vision"),
            _el("Plain text after the figure."),
        ]
    )
    vision = [c for c in res.chunks if "Model-generated" in c.text]
    assert len(vision) == 1
    assert vision[0].metadata["extraction"] == "vision"
    assert "Plain text" not in vision[0].text
    for c in res.chunks:
        if c is not vision[0]:
            assert c.metadata.get("extraction") != "vision"
            assert "Model-generated" not in c.text
    assert len(res.chunks) == 3


def test_vision_only_page_is_labelled_vision():
    res = _chunk([_el(VISION, extraction="vision")])
    assert len(res.chunks) == 1
    assert res.chunks[0].metadata["extraction"] == "vision"
    assert res.chunks[0].page == 1


def test_vision_after_heading_stays_separate_from_section_text():
    res = _chunk(
        [
            _el("Overview", type=ElementType.HEADING),
            _el("Section body text.", section="Overview"),
            _el(VISION, section="Overview", extraction="vision"),
        ]
    )
    assert [c.metadata.get("extraction") for c in res.chunks] == [None, "vision"]
    assert "Section body" not in res.chunks[1].text
    assert res.chunks[1].section == "Overview"


def test_ocr_chunks_never_cross_page_boundaries():
    res = _chunk(
        [
            _el("ocr text page one", page=1, extraction="ocr"),
            _el("ocr text page two", page=2, extraction="ocr"),
            _el("ocr text page three", page=3, extraction="ocr"),
        ],
        max_tokens=500,
    )
    assert [c.page for c in res.chunks] == [1, 2, 3]
    assert all(c.metadata["extraction"] == "ocr" for c in res.chunks)


def test_non_ocr_text_may_span_pages_and_takes_first_source_page():
    res = _chunk([_el("first words", page=1), _el("second words", page=2)], max_tokens=500)
    assert len(res.chunks) == 1
    assert res.chunks[0].page == 1
    assert "extraction" not in res.chunks[0].metadata


def test_page_comes_from_source_elements_not_chunk_index():
    # 4 elements -> 2 chunks. The old page_map[chunk_index] gave chunk 1 page 1.
    res = _chunk(
        [
            _el("Alpha", page=1, type=ElementType.HEADING),
            _el("alpha body", page=1, section="Alpha"),
            _el("Beta", page=5, type=ElementType.HEADING),
            _el("beta body", page=5, section="Beta"),
        ]
    )
    assert [(c.section, c.page) for c in res.chunks] == [("Alpha", 1), ("Beta", 5)]


def test_headings_and_section_path_preserved_across_runs():
    res = _chunk(
        [
            _el("Report", type=ElementType.TITLE),
            _el("Findings", type=ElementType.HEADING, section="Report"),
            _el("ocr body one", page=1, section="Findings", extraction="ocr"),
            _el("ocr body two", page=2, section="Findings", extraction="ocr"),
        ]
    )
    assert len(res.chunks) == 2
    for c in res.chunks:
        assert c.section == "Findings"
        assert c.metadata["headings"] == ["Report", "Findings"]
        assert "Findings" in c.text  # heading text stays searchable, as in builtin chunks
    assert [c.page for c in res.chunks] == [1, 2]


def test_table_header_repeats_on_every_table_chunk():
    rows = [["Quarter", "Revenue"]] + [
        [f"Q{i}", f"${i}0M across many markets"] for i in range(1, 13)
    ]
    st = {"rows": rows, "header_rows": 1, "num_rows": len(rows), "num_cols": 2}
    res = _chunk([_el("table", type=ElementType.TABLE, page=2, structured_table=st)], max_tokens=30)
    assert len(res.chunks) > 1
    for c in res.chunks:
        assert "Quarter" in c.text and "Revenue" in c.text
        assert "table" in c.element_types
        assert c.page == 2
        assert c.metadata["table_provenance"] == st
    body = " ".join(c.text for c in res.chunks)
    assert all(f"Q{i} " in body or f"Q{i}\n" in body or f"| Q{i} " in body for i in range(1, 13))


def test_markdown_only_table_is_parsed_as_a_table():
    md = "| Name | Qty |\n| --- | --- |\n" + "\n".join(
        f"| item{i} | {i} units here |" for i in range(15)
    )
    res = _chunk([_el(md, type=ElementType.TABLE)], max_tokens=25)
    assert len(res.chunks) > 1
    assert all("Name" in c.text and "Qty" in c.text for c in res.chunks)


def test_empty_document_yields_no_chunks():
    res = _chunk([])
    assert res.chunks == []
    assert _chunk([_el("   ")]).chunks == []


def test_heading_only_document_keeps_its_text():
    res = _chunk([_el("Just a title", type=ElementType.TITLE)])
    assert [c.text for c in res.chunks] == ["Just a title"]


def test_ids_order_and_parents_are_deterministic():
    elements = [
        _el("Big", type=ElementType.HEADING),
        _el(" ".join(f"w{i}" for i in range(120)), section="Big"),
        _el(VISION, extraction="vision"),
    ]
    first, second = _chunk(elements, 40), _chunk(elements, 40)
    assert [(c.id, c.text, c.parent_id, c.page) for c in first.chunks] == [
        (c.id, c.text, c.parent_id, c.page) for c in second.chunks
    ]
    assert [c.id for c in first.chunks] == [f"chunk-{i:04d}" for i in range(len(first.chunks))]
    assert [c.index for c in first.chunks] == list(range(len(first.chunks)))
    big = [c for c in first.chunks if c.section == "Big"]
    assert len(big) > 1
    assert big[0].parent_id is None
    assert all(c.parent_id == big[0].id for c in big[1:])
    assert first.chunks[-1].parent_id is None  # vision chunk is its own unit
    assert all(c.token_count <= 40 for c in first.chunks)


def test_chunking_makes_no_network_calls(monkeypatch):
    def _no_network(*args, **kwargs):
        raise AssertionError("network access at chunk time")

    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)
    res = _chunk([_el("Title", type=ElementType.TITLE), _el("body " * 80)], 30)
    assert res.chunks


@pytest.mark.parametrize("tokenizer", ["whitespace", "gemini", lambda t: len(t.split())])
def test_every_tokenizer_splits_oversized_plain_text(tokenizer):
    # semchunk needs a callable counter from get_tokenizer(); exercise that path.
    parsed = ParsedDocument(elements=[_el("word " * 200)], language="en")
    res = chunk_document_hybrid(parsed, max_tokens=40, tokenizer=tokenizer)
    assert len(res.chunks) > 1
    assert all(0 < c.token_count <= 40 for c in res.chunks)
