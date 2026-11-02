"""T-OR-16: Multimodal Gemini Embedding Profile (OpenRouter 3072-d).

Provides multimodal embedding capabilities for admitted image/page inputs
using paid Google Gemini Embedding 2, preserving citable visual provenance
(page, figure_id, bounding box) in a dedicated vector index.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from models.catalog import _lookup_admitted
from models.interfaces import LLMProvider
from models.policy import ModelRole
from models.vision import ImageContentPart, VisualCitation

from retrieval.interfaces import VectorRecord
from retrieval.serving import serving_alias_name

DEFAULT_MULTIMODAL_EMBEDDING_MODEL = "google/gemini-embedding-2"
MULTIMODAL_VECTOR_DIMENSION = 3072
DEFAULT_MULTIMODAL_COLLECTION = "knowledge_multimodal"


class MultimodalVectorRecord(VectorRecord):
    """Vector record with visual citation provenance."""

    visual_citation: VisualCitation | None = None
    media_type: str = "image/png"


@dataclass
class MultimodalEmbeddingProfile:
    """Manages multimodal embedding generation and dedicated vector store profiles."""

    model_name: str = DEFAULT_MULTIMODAL_EMBEDDING_MODEL
    dimension: int = MULTIMODAL_VECTOR_DIMENSION
    collection_name: str = DEFAULT_MULTIMODAL_COLLECTION
    serving_alias: str = field(init=False)
    provider: LLMProvider | None = None

    def __post_init__(self) -> None:
        self.serving_alias = serving_alias_name(self.collection_name)
        # Policy verification: verify model is admitted for embedding
        _lookup_admitted(ModelRole.EMBEDDING, self.model_name)

    async def embed_image_part(
        self,
        part: ImageContentPart,
        *,
        workspace_id: str,
        document_id: str,
        record_id: str,
        document_version_id: str | None = None,
    ) -> MultimodalVectorRecord:
        """Embed an image part and attach its visual citation.

        Fails loudly (no placeholder vectors) on a missing image, a missing or
        incapable provider, an empty vector or a wrong dimension.
        """
        url = part.image_url.get("url", "")
        if not url or url.endswith("base64,"):
            raise ValueError("no image data in the content part; refusing to embed")
        citation = VisualCitation(
            page=part.page,
            figure_id=part.figure_id,
            bbox=part.bbox,
        )

        embedding_vector = await self._call_provider_embed(part)
        if len(embedding_vector) != self.dimension:
            raise ValueError(
                f"multimodal embedding has {len(embedding_vector)} dimensions, "
                f"expected {self.dimension}"
            )

        return MultimodalVectorRecord(
            id=record_id,
            vector=embedding_vector,
            content=f"[Image page={part.page or 1} figure={part.figure_id or 'none'}]",
            workspace_id=workspace_id,
            document_id=document_id,
            document_version_id=document_version_id,
            visual_citation=citation,
            media_type=part.media_type,
            metadata={
                "page": part.page,
                "figure_id": part.figure_id,
                "embedding_model": self.model_name,
                "embedding_dimension": self.dimension,
                "multimodal": True,
            },
        )

    async def _call_provider_embed(self, part: ImageContentPart) -> list[float]:
        """Call the provider's ``embed_multimodal`` — no fallbacks, no fake vectors.

        ``embed_multimodal`` is an OPTIONAL duck-typed extension on a concrete
        provider (not on the ``LLMProvider`` ABC), so it is probed at runtime.
        """
        provider = self.provider
        if provider is None:
            raise RuntimeError("multimodal embedding requires a provider; none configured")
        embed_multimodal = getattr(provider, "embed_multimodal", None)
        if not callable(embed_multimodal):
            raise RuntimeError(
                f"provider {type(provider).__name__} does not implement embed_multimodal"
            )
        vectors: list[float] = await embed_multimodal(part)
        return vectors
