"""Vision (model-generated) text keeps exact chunk provenance and is labelled on citations."""

from __future__ import annotations

import asyncio

import pytest
from answer.citation_integrity import CandidateCitation, validate_citation_integrity
from answer.models import CanonicalChunk, Claim, DocumentStatus
from answer.verification import verify_claims
from ingestion.chunk import chunk_elements
from ingestion.interfaces import DocumentElement, ElementType
from retrieval.interfaces import RetrievedItem

pytestmark = pytest.mark.unit

VISION = "[Model-generated page description, page 1] A bar chart of sales."


def _el(text, *, page=1, section=None, extraction=None, kind=ElementType.TEXT):
    meta = {"extraction": extraction} if extraction else {}
    return DocumentElement(type=kind, text=text, page=page, section=section, metadata=meta)


def _ext(chunks):
    return [c.metadata.get("extraction") for c in chunks]


def test_mixed_vision_ocr_text_on_one_page_never_merge():
    chunks = chunk_elements(
        [
            _el("plain text"),
            _el("ocr text", extraction="tesseract"),
            _el(VISION, extraction="vision"),
            _el("more ocr", extraction="tesseract"),
        ]
    )
    vision = [c for c in chunks if "Model-generated" in c.text]
    assert len(vision) == 1 and vision[0].metadata["extraction"] == "vision"
    assert "ocr" not in vision[0].text and "plain" not in vision[0].text
    for c in chunks:
        if c is not vision[0]:
            assert c.metadata.get("extraction") != "vision"
            assert "Model-generated" not in c.text


def test_vision_only_page():
    chunks = chunk_elements([_el(VISION, extraction="vision")])
    assert len(chunks) == 1 and chunks[0].metadata["extraction"] == "vision"


def test_empty_metadata_elements_unchanged():
    chunks = chunk_elements([_el("a b"), _el("c d")])
    assert len(chunks) == 1 and "extraction" not in chunks[0].metadata


def test_unknown_extraction_value_passes_through_and_merges_with_ocr():
    chunks = chunk_elements([_el("a", extraction="weird"), _el("b", extraction="tesseract")])
    assert _ext(chunks) == ["weird"]


def test_vision_after_heading_section_still_separate():
    chunks = chunk_elements(
        [
            _el("H", kind=ElementType.HEADING, section="S"),
            _el("body", section="S"),
            _el(VISION, section="S", extraction="vision"),
        ]
    )
    assert _ext(chunks) == [None, "vision"]


def _item(extraction):
    meta = {"extraction": extraction} if extraction else {}
    return RetrievedItem(
        content="The chart shows sales.",
        score=1.0,
        workspace_id="ws",
        document_id="d",
        chunk_id="c1",
        page=1,
        metadata=meta,
    )


@pytest.mark.parametrize("label", [None, "tesseract"])
def test_verify_claims_citation_exposes_extraction(label):
    _, cits = asyncio.run(
        verify_claims([Claim(text="The chart shows sales.", chunk_ids=["c1"])], [_item(label)])
    )
    assert cits[0].extraction == label


def test_verify_claims_vision_only_citation_is_not_verified():
    # Vision-only support is never verified (see test_vision_citation_policy.py).
    verified, cits = asyncio.run(
        verify_claims([Claim(text="The chart shows sales.", chunk_ids=["c1"])], [_item("vision")])
    )
    assert verified == [] and cits == []


def test_stage11_citation_exposes_extraction_and_tolerates_junk():
    def run(meta):
        chunk = CanonicalChunk(
            chunk_id="c1",
            workspace_id="ws",
            document_id="d",
            filename="f.pdf",
            page=1,
            content="The chart shows sales.",
            status=DocumentStatus.ACTIVE,
            metadata=meta,
        )
        return asyncio.run(
            validate_citation_integrity(
                claims=[Claim(text="The chart shows sales.", chunk_ids=["c1"])],
                citations=[CandidateCitation(chunk_id="c1", claim_text="The chart shows sales.")],
                canonical={"c1": chunk},
                allowed_workspace_ids={"ws"},
            )
        )

    vision = run({"extraction": "vision"})  # vision-only: rejected, never accepted
    assert vision.accepted_citations == []
    assert vision.rejected_citations[0].citation.chunk_id == "c1"
    assert run({}).accepted_citations[0].extraction is None
    assert run({"extraction": 5}).accepted_citations[0].extraction is None
