"""Unit test suite for typed image messages & vision adapter (T-OR-11).

Verifies:
- TextContentPart and ImageContentPart modeling, base64 data URIs, and detail levels
- Maximum image payload limit enforcement
- MultimodalMessage serialization to OpenAI/OpenRouter compatible format
- to_content_parts migration helper from strings/dicts
- VisualCitation extraction from image parts with page/figure/bbox provenance
- OpenRouterVisionAdapter free -> paid fallback
- Sensitive workspace fail-closed guard
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from models.interfaces import MessageRole
from models.vision import (
    BoundingBox,
    ImageContentPart,
    ImageDetail,
    MultimodalMessage,
    OpenRouterVisionAdapter,
    TextContentPart,
    VisionGenerationRequest,
    VisionPolicyError,
    to_content_parts,
)

pytestmark = pytest.mark.unit


def test_text_and_image_content_parts() -> None:
    text_part = TextContentPart(text="Analyze the graph in figure 2.")
    assert text_part.type == "text"
    assert text_part.text == "Analyze the graph in figure 2."

    # Test image from bytes
    sample_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    bbox = BoundingBox(x0=0.1, y0=0.2, x1=0.8, y1=0.9)
    img_part = ImageContentPart.from_bytes(
        sample_bytes,
        media_type="image/png",
        page=3,
        figure_id="fig-arch-01",
        bbox=bbox,
        detail=ImageDetail.HIGH,
    )

    assert img_part.type == "image_url"
    assert img_part.page == 3
    assert img_part.figure_id == "fig-arch-01"
    assert img_part.bbox == bbox
    assert img_part.detail == ImageDetail.HIGH
    assert img_part.image_url["url"].startswith("data:image/png;base64,")


def test_image_payload_limit_enforced() -> None:
    large_bytes = b"x" * (6 * 1024 * 1024)  # 6 MiB (exceeds 5 MiB ceiling)
    with pytest.raises(ValueError, match="exceeds maximum allowed"):
        ImageContentPart.from_bytes(large_bytes)


def test_multimodal_message_serialization() -> None:
    msg = MultimodalMessage(
        role=MessageRole.USER,
        parts=[
            TextContentPart(text="Describe this invoice"),
            ImageContentPart.from_uri("https://example.com/invoice.png", page=1),
        ],
    )

    serialized = msg.to_chat_message_dict()
    assert serialized["role"] == "user"
    assert len(serialized["content"]) == 2
    assert serialized["content"][0] == {"type": "text", "text": "Describe this invoice"}
    assert serialized["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "https://example.com/invoice.png", "detail": "auto"},
    }


def test_to_content_parts_migration_helper() -> None:
    # String input
    parts = to_content_parts("Legacy query string")
    assert len(parts) == 1
    assert isinstance(parts[0], TextContentPart)
    assert parts[0].text == "Legacy query string"

    # Dict input
    dict_parts = to_content_parts(
        [
            {"type": "text", "text": "Header"},
            {"type": "image_url", "image_url": {"url": "https://img.com/a.jpg"}, "page": 4},
        ]
    )
    assert len(dict_parts) == 2
    assert isinstance(dict_parts[0], TextContentPart)
    assert isinstance(dict_parts[1], ImageContentPart)
    assert dict_parts[1].page == 4


@pytest.mark.asyncio
async def test_vision_adapter_standard_workspace_free_route() -> None:
    mock_transport = AsyncMock()
    mock_transport.post_completion.return_value = {
        "choices": [{"message": {"content": "Identified Table 1 on page 2."}}]
    }

    adapter = OpenRouterVisionAdapter(transport=mock_transport)
    req = VisionGenerationRequest(
        messages=[
            MultimodalMessage(
                role=MessageRole.USER,
                parts=[
                    TextContentPart(text="Read table"),
                    ImageContentPart.from_uri(
                        "https://example.com/t.png", page=2, figure_id="tbl-1"
                    ),
                ],
            )
        ],
        workspace_id="ws-public-01",
        is_sensitive=False,
    )

    res = await adapter.generate_vision(req)
    assert res.text == "Identified Table 1 on page 2."
    assert res.model == "google/gemini-2.5-flash:free"
    assert res.is_paid is False
    assert len(res.citations) == 1
    assert res.citations[0].page == 2
    assert res.citations[0].figure_id == "tbl-1"


@pytest.mark.asyncio
async def test_vision_adapter_free_exhaustion_paid_fallback() -> None:
    mock_transport = AsyncMock()
    # First call fails (rate limit 429), second succeeds on paid fallback
    mock_transport.post_completion.side_effect = [
        RuntimeError("429 Rate limit exceeded on free pool"),
        {"choices": [{"message": {"content": "Paid fallback response."}}]},
    ]

    adapter = OpenRouterVisionAdapter(transport=mock_transport)
    req = VisionGenerationRequest(
        messages=[
            MultimodalMessage(
                role=MessageRole.USER,
                parts=[TextContentPart(text="Read chart")],
            )
        ],
        workspace_id="ws-public-02",
        is_sensitive=False,
        allow_paid_fallback=True,
    )

    res = await adapter.generate_vision(req)
    assert res.text == "Paid fallback response."
    assert res.model == "google/gemini-2.5-flash"
    assert res.is_paid is True
    assert res.metadata.get("fallback_from") == "google/gemini-2.5-flash:free"


@pytest.mark.asyncio
async def test_vision_adapter_sensitive_workspace_guard() -> None:
    mock_transport = AsyncMock()
    adapter = OpenRouterVisionAdapter(transport=mock_transport)

    # Sensitive workspace without paid fallback authorization must fail closed
    req_denied = VisionGenerationRequest(
        messages=[
            MultimodalMessage(
                role=MessageRole.USER, parts=[TextContentPart(text="Classified doc")]
            )
        ],
        workspace_id="ws-sensitive-confidential",
        is_sensitive=True,
        allow_paid_fallback=False,
    )

    with pytest.raises(VisionPolicyError, match="prohibits free cloud vision endpoints"):
        await adapter.generate_vision(req_denied)

    # Sensitive workspace with paid authorization routes directly to paid model
    mock_transport.post_completion.return_value = {
        "choices": [{"message": {"content": "Classified document analyzed."}}]
    }
    req_allowed = VisionGenerationRequest(
        messages=[
            MultimodalMessage(
                role=MessageRole.USER, parts=[TextContentPart(text="Classified doc")]
            )
        ],
        workspace_id="ws-sensitive-confidential",
        is_sensitive=True,
        allow_paid_fallback=True,
    )
    res = await adapter.generate_vision(req_allowed)
    assert res.model == "google/gemini-2.5-flash"
    assert res.is_paid is True
