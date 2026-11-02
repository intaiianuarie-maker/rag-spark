"""Unit tests for T-OR-16: Multimodal Gemini Embedding Profile."""

from __future__ import annotations

import asyncio
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


def _embed(profile: MultimodalEmbeddingProfile, part: ImageContentPart, **kw):
    return asyncio.run(
        profile.embed_image_part(
            part,
            workspace_id="ws-mm",
            document_id="doc-1",
            record_id="rec-1",
            **kw,
        )
    )


def test_multimodal_embedding_profile_initialization() -> None:
    profile = MultimodalEmbeddingProfile()
    assert profile.model_name == DEFAULT_MULTIMODAL_EMBEDDING_MODEL
    assert profile.dimension == MULTIMODAL_VECTOR_DIMENSION
    assert profile.collection_name == DEFAULT_MULTIMODAL_COLLECTION
    assert profile.serving_alias == f"{DEFAULT_MULTIMODAL_COLLECTION}__serving"


def test_embed_image_part_preserves_visual_citation_and_version() -> None:
    provider = AsyncMock()
    provider.embed_multimodal.return_value = [0.25] * 3072
    profile = MultimodalEmbeddingProfile(provider=provider)
    bbox = BoundingBox(x0=0.1, y0=0.1, x1=0.9, y1=0.8)
    part = ImageContentPart.from_bytes(
        b"png-bytes", media_type="image/png", page=3, figure_id="chart-4", bbox=bbox
    )

    record = _embed(profile, part, document_version_id="ver-9")

    assert isinstance(record, MultimodalVectorRecord)
    assert record.id == "rec-1"
    assert len(record.vector) == 3072
    assert record.workspace_id == "ws-mm"
    assert record.document_id == "doc-1"
    assert record.document_version_id == "ver-9"
    assert record.visual_citation is not None
    assert record.visual_citation.page == 3
    assert record.visual_citation.figure_id == "chart-4"
    assert record.visual_citation.bbox == bbox
    assert record.metadata["multimodal"] is True


def test_embed_image_part_calls_provider_when_present() -> None:
    mock_provider = AsyncMock()
    custom_vector = [0.123] * 3072
    mock_provider.embed_multimodal.return_value = custom_vector
    profile = MultimodalEmbeddingProfile(provider=mock_provider)
    part = ImageContentPart.from_uri("https://example.com/fig.png", page=1, figure_id="fig-1")

    record = _embed(profile, part)

    assert record.vector == custom_vector
    assert mock_provider.embed_multimodal.called


PART = ImageContentPart.from_bytes(b"png-bytes", media_type="image/png", page=1)


def test_no_provider_fails_loudly_instead_of_inventing_a_vector() -> None:
    with pytest.raises(RuntimeError, match="provider"):
        _embed(MultimodalEmbeddingProfile(), PART)


def test_provider_without_embed_multimodal_fails_loudly() -> None:
    class TextOnly:
        async def embed_texts(self, texts):  # must NOT be used as a fake fallback
            return [[0.0] * 3072]

    with pytest.raises(RuntimeError, match="embed_multimodal"):
        _embed(MultimodalEmbeddingProfile(provider=TextOnly()), PART)  # type: ignore[arg-type]


def test_provider_error_propagates() -> None:
    provider = AsyncMock()
    provider.embed_multimodal.side_effect = RuntimeError("api down")
    with pytest.raises(RuntimeError, match="api down"):
        _embed(MultimodalEmbeddingProfile(provider=provider), PART)


@pytest.mark.parametrize("vec", [[], [0.1] * 768])
def test_empty_or_wrong_dimension_vector_raises(vec) -> None:
    provider = AsyncMock()
    provider.embed_multimodal.return_value = vec
    with pytest.raises(ValueError):
        _embed(MultimodalEmbeddingProfile(provider=provider), PART)


@pytest.mark.parametrize(
    "part",
    [
        ImageContentPart.from_bytes(b"", media_type="image/png"),
        ImageContentPart(image_url={}),
    ],
)
def test_part_without_image_bytes_is_never_embedded(part) -> None:
    provider = AsyncMock()
    with pytest.raises(ValueError, match="no image"):
        _embed(MultimodalEmbeddingProfile(provider=provider), part)
    provider.embed_multimodal.assert_not_called()
