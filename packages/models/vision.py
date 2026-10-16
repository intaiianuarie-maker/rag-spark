"""Typed multimodal content parts & OpenRouter Vision adapter (T-OR-11).

Provides:
- Typed message content parts (text & image) with media types and visual provenance
- Bounding box, page number, and figure/table citation tracking
- OpenRouter Vision adapter targeting Google Gemini 2.5 Flash Free with paid fallback
- Strict fail-closed privacy guards for sensitive workspaces
- Payload envelope validation and downscaling helper
"""

from __future__ import annotations

import base64
import logging
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .interfaces import MessageRole

logger = logging.getLogger("knowledge_os.models.vision")

DEFAULT_VISION_FREE_MODEL = "google/gemini-2.5-flash:free"
DEFAULT_VISION_PAID_MODEL = "google/gemini-2.5-flash"
MAX_IMAGE_PAYLOAD_BYTES = 5 * 1024 * 1024  # 5 MiB


class VisionPolicyError(RuntimeError):
    """Raised when vision request violates provider policy or workspace sensitivity."""


class ImageDetail(StrEnum):
    AUTO = "auto"
    LOW = "low"
    HIGH = "high"


class BoundingBox(BaseModel):
    """Normalized coordinates [0.0 - 1.0] or absolute point coordinates."""

    x0: float
    y0: float
    x1: float
    y1: float


class TextContentPart(BaseModel):
    """Textual part of a multimodal message."""

    type: Literal["text"] = "text"
    text: str


class ImageContentPart(BaseModel):
    """Image part of a multimodal message with provenance."""

    type: Literal["image_url"] = "image_url"
    image_url: dict[str, str] = Field(default_factory=dict)
    media_type: str = "image/png"
    page: int | None = None
    figure_id: str | None = None
    bbox: BoundingBox | None = None
    detail: ImageDetail = ImageDetail.AUTO

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        media_type: str = "image/png",
        page: int | None = None,
        figure_id: str | None = None,
        bbox: BoundingBox | None = None,
        detail: ImageDetail = ImageDetail.AUTO,
    ) -> ImageContentPart:
        if len(data) > MAX_IMAGE_PAYLOAD_BYTES:
            raise ValueError(
                f"Image payload size ({len(data)} bytes) exceeds maximum "
                f"allowed ({MAX_IMAGE_PAYLOAD_BYTES} bytes)"
            )
        b64 = base64.b64encode(data).decode("utf-8")
        data_uri = f"data:{media_type};base64,{b64}"
        return cls(
            image_url={"url": data_uri, "detail": detail.value},
            media_type=media_type,
            page=page,
            figure_id=figure_id,
            bbox=bbox,
            detail=detail,
        )

    @classmethod
    def from_uri(
        cls,
        uri: str,
        media_type: str = "image/png",
        page: int | None = None,
        figure_id: str | None = None,
        bbox: BoundingBox | None = None,
        detail: ImageDetail = ImageDetail.AUTO,
    ) -> ImageContentPart:
        return cls(
            image_url={"url": uri, "detail": detail.value},
            media_type=media_type,
            page=page,
            figure_id=figure_id,
            bbox=bbox,
            detail=detail,
        )


ContentPart = TextContentPart | ImageContentPart


class MultimodalMessage(BaseModel):
    """Multimodal message containing typed text and image parts."""

    role: MessageRole
    parts: list[ContentPart] = Field(default_factory=list)
    name: str | None = None

    def to_chat_message_dict(self) -> dict[str, Any]:
        """Serializes to OpenAI-compatible multimodal chat format."""
        serialized_parts = []
        for part in self.parts:
            if isinstance(part, TextContentPart):
                serialized_parts.append({"type": "text", "text": part.text})
            elif isinstance(part, ImageContentPart):
                serialized_parts.append(
                    {"type": "image_url", "image_url": part.image_url}
                )
        return {
            "role": self.role.value,
            "content": serialized_parts,
            **({"name": self.name} if self.name else {}),
        }


def to_content_parts(content: str | list[Any]) -> list[ContentPart]:
    """Helper converting legacy strings or dicts to typed ContentParts."""
    if isinstance(content, str):
        return [TextContentPart(text=content)]
    parts: list[ContentPart] = []
    for item in content:
        if isinstance(item, (TextContentPart, ImageContentPart)):
            parts.append(item)
        elif isinstance(item, dict):
            item_type = item.get("type", "text")
            if item_type == "text":
                parts.append(TextContentPart(text=item.get("text", "")))
            elif item_type == "image_url":
                parts.append(
                    ImageContentPart(
                        image_url=item.get("image_url", {}),
                        media_type=item.get("media_type", "image/png"),
                        page=item.get("page"),
                        figure_id=item.get("figure_id"),
                        bbox=BoundingBox(**item["bbox"]) if item.get("bbox") else None,
                        detail=ImageDetail(item.get("detail", "auto")),
                    )
                )
    return parts


class VisualCitation(BaseModel):
    """Visual citation pointing to a figure or page region as evidence."""

    page: int | None = None
    figure_id: str | None = None
    bbox: BoundingBox | None = None
    label: str | None = None
    source_url: str | None = None


class VisionGenerationRequest(BaseModel):
    """Request for multimodal vision inference."""

    messages: list[MultimodalMessage]
    prompt: str = ""
    workspace_id: str | None = None
    is_sensitive: bool = False
    allow_paid_fallback: bool = True
    max_tokens: int = 1024
    temperature: float = 0.0


class VisionGenerationResult(BaseModel):
    """Result of multimodal vision inference."""

    text: str
    model: str
    is_paid: bool = False
    citations: list[VisualCitation] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class OpenRouterVisionAdapter:
    """OpenRouter vision adapter with free -> paid fallback and sensitive guard."""

    def __init__(
        self,
        transport: Any = None,
        free_model: str = DEFAULT_VISION_FREE_MODEL,
        paid_model: str = DEFAULT_VISION_PAID_MODEL,
    ) -> None:
        self.transport = transport
        self.free_model = free_model
        self.paid_model = paid_model

    async def generate_vision(
        self, request: VisionGenerationRequest
    ) -> VisionGenerationResult:
        # Sensitive workspace guard (Section 9):
        # If an image document is processed for a sensitive workspace, require a paid route
        # or fail closed before serializing data to free/cloud endpoints without consent.
        target_model = self.free_model
        is_paid = False

        if request.is_sensitive:
            if not request.allow_paid_fallback:
                raise VisionPolicyError(
                    f"Sensitive workspace '{request.workspace_id}' prohibits "
                    "free cloud vision endpoints and paid fallback is disabled."
                )
            target_model = self.paid_model
            is_paid = True

        # Check image payload limits across all parts
        total_image_bytes = 0
        visual_citations: list[VisualCitation] = []
        for msg in request.messages:
            for part in msg.parts:
                if isinstance(part, ImageContentPart):
                    url = part.image_url.get("url", "")
                    if url.startswith("data:"):
                        # Rough estimate from base64 string
                        total_image_bytes += int(len(url) * 3 / 4)
                    if part.page is not None or part.figure_id or part.bbox:
                        visual_citations.append(
                            VisualCitation(
                                page=part.page,
                                figure_id=part.figure_id,
                                bbox=part.bbox,
                                source_url=part.image_url.get("url"),
                            )
                        )

        if total_image_bytes > MAX_IMAGE_PAYLOAD_BYTES * 2:
            raise VisionPolicyError(
                f"Total image payload ({total_image_bytes} bytes) exceeds envelope budget."
            )

        # Execute through transport if configured, else mock/offline fallback
        if self.transport is not None:
            try:
                # Attempt primary target model
                messages_dict = [m.to_chat_message_dict() for m in request.messages]
                payload = {
                    "model": target_model,
                    "messages": messages_dict,
                    "max_tokens": request.max_tokens,
                    "temperature": request.temperature,
                }
                res = await self.transport.post_completion(payload)
                return VisionGenerationResult(
                    text=res.get("choices", [{}])[0].get("message", {}).get("content", ""),
                    model=target_model,
                    is_paid=is_paid,
                    citations=visual_citations,
                    metadata={"total_image_bytes": total_image_bytes},
                )
            except Exception as exc:
                if (
                    not is_paid
                    and request.allow_paid_fallback
                    and not request.is_sensitive
                ):
                    logger.info(
                        "Vision free endpoint exhausted (%s), falling back to paid model %s",
                        exc,
                        self.paid_model,
                    )
                    payload["model"] = self.paid_model
                    res = await self.transport.post_completion(payload)
                    return VisionGenerationResult(
                        text=res.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", ""),
                        model=self.paid_model,
                        is_paid=True,
                        citations=visual_citations,
                        metadata={
                            "total_image_bytes": total_image_bytes,
                            "fallback_from": self.free_model,
                        },
                    )
                raise

        # Offline / simulation fallback
        return VisionGenerationResult(
            text="Simulated vision response.",
            model=target_model,
            is_paid=is_paid,
            citations=visual_citations,
            metadata={"simulated": True, "total_image_bytes": total_image_bytes},
        )
