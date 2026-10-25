"""Lazy cross-encoder SDK construction for the optional rerank sidecar."""

from __future__ import annotations

from typing import Any


def load_cross_encoder(model_name: str) -> Any:
    from sentence_transformers import CrossEncoder

    return CrossEncoder(model_name, max_length=512, device="cpu")
