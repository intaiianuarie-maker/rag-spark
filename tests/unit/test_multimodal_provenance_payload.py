"""Multimodal points persist page/media_type/visual_citation in the Qdrant payload.

Real in-memory Qdrant (plain sync; asyncio.run), read back through ``client.retrieve``.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
from models.vision import BoundingBox, ImageContentPart
from qdrant_client import AsyncQdrantClient
from retrieval.adapters.multimodal_embed import MultimodalEmbeddingProfile
from retrieval.adapters.qdrant_store import QdrantVectorStore
from retrieval.embedding_identity import EmbeddingIdentity, merge_identity
from retrieval.interfaces import VectorRecord

pytestmark = pytest.mark.unit

MM = "knowledge_multimodal"
WS, DOC, VER = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
IDENT = EmbeddingIdentity("openrouter", "google/gemini-embedding-2", "r1", 2)


def _roundtrip(part: ImageContentPart, record_id: str | None = None) -> dict:
    """Embed ``part`` (2-d), upsert via the real store, return the stored payload."""
    rid = record_id or str(uuid.uuid4())

    async def go() -> dict:
        provider = AsyncMock()
        provider.embed_multimodal.return_value = [1.0, 0.5]
        profile = MultimodalEmbeddingProfile(provider=provider, dimension=2)
        record = await profile.embed_image_part(
            part, workspace_id=WS, document_id=DOC, document_version_id=VER, record_id=rid
        )
        record.metadata = merge_identity(record.metadata, IDENT)  # as the pipeline does
        client = AsyncQdrantClient(location=":memory:")
        store = QdrantVectorStore(client=client, collection=MM)
        await store.ensure_collection(collection=MM, dimension=2)
        assert await store.upsert(collection=MM, records=[record]) == 1
        (point,) = await client.retrieve(MM, ids=[str(uuid.UUID(rid))], with_payload=True)
        return point.payload

    return asyncio.run(go())


def test_payload_carries_page_media_type_and_visual_citation() -> None:
    bbox = BoundingBox(x0=0.1, y0=0.2, x1=0.9, y1=0.8)
    part = ImageContentPart.from_bytes(
        b"png", media_type="image/jpeg", page=3, figure_id="chart-4", bbox=bbox
    )
    p = _roundtrip(part)
    assert p["page"] == 3
    assert p["metadata"]["media_type"] == "image/jpeg"
    assert p["metadata"]["visual_citation"] == {
        "page": 3,
        "figure_id": "chart-4",
        "bbox": {"x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.8},
    }
    # tenant/version scoping keys are untouched
    assert (p["workspace_id"], p["document_id"], p["document_version_id"]) == (WS, DOC, VER)


def test_figure_id_and_bbox_none() -> None:
    p = _roundtrip(ImageContentPart.from_bytes(b"png", page=2))
    assert p["page"] == 2
    assert p["metadata"]["visual_citation"] == {"page": 2, "figure_id": None, "bbox": None}


def test_page_only_record_without_visual_citation_fields() -> None:
    p = _roundtrip(ImageContentPart.from_bytes(b"png"))  # no page at all
    assert "page" not in p  # same rule as text points: None is not stored
    assert p["metadata"]["visual_citation"] == {"page": None, "figure_id": None, "bbox": None}
    assert p["metadata"]["media_type"] == "image/png"


def test_text_record_payload_is_unchanged() -> None:
    async def go() -> dict:
        client = AsyncQdrantClient(location=":memory:")
        store = QdrantVectorStore(client=client, collection="t")
        await store.ensure_collection(collection="t", dimension=2)
        rid = str(uuid.uuid4())
        rec = VectorRecord(
            id=rid, vector=[1.0, 0.5], content="hello", workspace_id=WS,
            document_id=DOC, document_version_id=VER, page=7,
            metadata=merge_identity({"k": "v"}, IDENT),
        )
        await store.upsert(collection="t", records=[rec])
        (pt,) = await client.retrieve("t", ids=[str(uuid.UUID(rid))], with_payload=True)
        return pt.payload

    assert asyncio.run(go()) == {
        "workspace_id": WS, "content": "hello", "metadata": merge_identity({"k": "v"}, IDENT),
        "document_id": DOC, "document_version_id": VER, "page": 7,
    }
