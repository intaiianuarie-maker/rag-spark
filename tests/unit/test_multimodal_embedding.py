"""Unit tests for T-OR-16: Multimodal Gemini Embedding Profile."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from models.vision import BoundingBox, ImageContentPart
from retrieval.adapters.multimodal_embed import (
    DEFAULT_MULTIMODAL_COLLECTION,
    DEFAULT_MULTIMODAL_EMBEDDING_MODEL,
    MULTIMODAL_VECTOR_DIMENSION,
    MultimodalEmbeddingProfile,
    MultimodalVectorRecord,
)

pytestmark = [pytest.mark.unit]


def test_multimodal_embedding_profile_initialization() -> None:
    profile = MultimodalEmbeddingProfile()
    assert profile.model_name == DEFAULT_MULTIMODAL_EMBEDDING_MODEL
    assert profile.dimension == MULTIMODAL_VECTOR_DIMENSION
    assert profile.collection_name == DEFAULT_MULTIMODAL_COLLECTION
    assert profile.serving_alias == f"{DEFAULT_MULTIMODAL_COLLECTION}__serving"


@pytest.mark.asyncio
async def test_embed_image_part_preserves_visual_citation() -> None:
    profile = MultimodalEmbeddingProfile()
    bbox = BoundingBox(x0=0.1, y0=0.1, x1=0.9, y1=0.8)
    part = ImageContentPart.from_bytes(
        b"fake_png_data",
        media_type="image/png",
        page=3,
        figure_id="chart-4",
        bbox=bbox,
    )

    record = await profile.embed_image_part(
        part,
        workspace_id="ws-multimodal-test",
        document_id="doc-report-1",
        record_id="rec-img-001",
    )

    assert isinstance(record, MultimodalVectorRecord)
    assert record.id == "rec-img-001"
    assert len(record.vector) == 3072
    assert record.workspace_id == "ws-multimodal-test"
    assert record.visual_citation is not None
    assert record.visual_citation.page == 3
    assert record.visual_citation.figure_id == "chart-4"
    assert record.visual_citation.bbox == bbox
    assert record.metadata["multimodal"] is True


@pytest.mark.asyncio
async def test_embed_image_part_calls_provider_when_present() -> None:
    mock_provider = AsyncMock()
    custom_vector = [0.123] * 3072
    mock_provider.embed_multimodal.return_value = custom_vector

    profile = MultimodalEmbeddingProfile(provider=mock_provider)
    part = ImageContentPart.from_uri("https://example.com/fig.png", page=1, figure_id="fig-1")

    record = await profile.embed_image_part(
        part,
        workspace_id="ws-mm",
        document_id="doc-1",
        record_id="rec-1",
    )

    assert record.vector == custom_vector
    assert mock_provider.embed_multimodal.called
