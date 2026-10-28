"""FastAPI application for standalone MiniLM rerank sidecar (T-ARCH-06).

Specifications:
- Model: cross-encoder/ms-marco-MiniLM-L6-v2 (~100MB weights).
- CPU-only execution.
- TORCH_NUM_THREADS clamped to 1-2 (default 1).
- Endpoints:
    * GET /health : Process liveness.
    * GET /ready  : Readiness-as-capability (checks model loaded & operable).
    * POST /rerank: Cross-encoder rerank API (OpenAI/Jina compatible).
    * GET /metrics: Resource and memory RSS observation.
"""

from __future__ import annotations

import logging
import os
import resource
import time
from contextlib import asynccontextmanager
from typing import Any

import torch
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from .adapters.cross_encoder import load_cross_encoder

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rerank_sidecar")

# Thread clamping: 1-2 per T-ARCH-06 specification
DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L6-v2"
MODEL_NAME = os.environ.get("RERANK_MODEL", DEFAULT_MODEL)

raw_threads = int(os.environ.get("TORCH_NUM_THREADS", "1"))
NUM_THREADS = max(1, min(2, raw_threads))
torch.set_num_threads(NUM_THREADS)
os.environ["OMP_NUM_THREADS"] = str(NUM_THREADS)
os.environ["MKL_NUM_THREADS"] = str(NUM_THREADS)


class ModelCapabilityManager:
    """Manages the lifecycle and readiness of the MiniLM cross-encoder."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.model: Any = None
        self.load_error: str | None = None
        self.load_duration_seconds: float = 0.0
        self.query_count: int = 0
        self.total_pairs_scored: int = 0

    def load(self) -> None:
        """Load cross-encoder weights into memory on CPU."""
        t0 = time.perf_counter()
        try:
            logger.info(
                "Loading CrossEncoder model '%s' (threads=%d)...",
                self.model_name,
                NUM_THREADS,
            )
            self.model = load_cross_encoder(self.model_name)
            self.load_duration_seconds = time.perf_counter() - t0
            self.load_error = None
            logger.info("Model '%s' ready in %.2fs", self.model_name, self.load_duration_seconds)
        except Exception as exc:
            self.load_error = f"{type(exc).__name__}: {exc}"
            self.model = None
            logger.error("Failed to load model '%s': %s", self.model_name, self.load_error)

    @property
    def is_ready(self) -> bool:
        """Readiness-as-capability: True only if model is instantiated and operable."""
        return self.model is not None and self.load_error is None

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Compute cross-encoder relevance scores for query-document pairs."""
        if not self.is_ready:
            raise RuntimeError(f"Reranker is not ready: {self.load_error or 'unloaded'}")
        if not pairs:
            return []
        scores = self.model.predict(pairs)
        self.query_count += 1
        self.total_pairs_scored += len(pairs)
        return [float(s) for s in scores]


manager = ModelCapabilityManager(MODEL_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load model capability on startup."""
    manager.load()
    yield


app = FastAPI(
    title="Standalone MiniLM Rerank Sidecar",
    description="Lightweight CPU-only cross-encoder sidecar for Super RAG (T-ARCH-06)",
    version="1.0.0",
    lifespan=lifespan,
)


# --- Request and Response Schemas ---


class RerankRequest(BaseModel):
    query: str = Field(..., description="Search query string")
    documents: list[str] = Field(..., description="List of document passage contents to score")
    model: str | None = Field(default=None, description="Optional model identifier override")
    top_n: int | None = Field(
        default=None, description="Maximum number of ranked results to return"
    )


class RerankResultItem(BaseModel):
    index: int = Field(..., description="0-based index of document in input list")
    relevance_score: float = Field(..., description="Cross-encoder relevance logit/score")


class RerankResponse(BaseModel):
    model: str = Field(..., description="Model used for scoring")
    results: list[RerankResultItem] = Field(..., description="Ranked document items, descending")


class ReadyResponse(BaseModel):
    ready: bool
    model: str
    device: str
    torch_num_threads: int
    load_duration_seconds: float
    peak_rss_mb: float


# --- Routes ---


@app.get("/health", status_code=status.HTTP_200_OK)
async def health() -> dict[str, str]:
    """Process liveness probe."""
    return {"status": "ok"}


@app.get("/ready", response_model=ReadyResponse)
async def ready() -> ReadyResponse:
    """Readiness-as-capability probe.
    
    Returns 200 OK only when the model is loaded in memory and capable of scoring.
    Returns 503 Service Unavailable when model is not ready.
    """
    if not manager.is_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"ready": False, "error": manager.load_error or "model_unloaded"},
        )
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    return ReadyResponse(
        ready=True,
        model=manager.model_name,
        device="cpu",
        torch_num_threads=torch.get_num_threads(),
        load_duration_seconds=manager.load_duration_seconds,
        peak_rss_mb=rss_mb,
    )


@app.post("/rerank", response_model=RerankResponse)
async def rerank(request: RerankRequest) -> RerankResponse:
    """Cross-encoder rerank endpoint."""
    if not manager.is_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Reranker unavailable: {manager.load_error or 'model not loaded'}",
        )

    if not request.documents:
        return RerankResponse(model=manager.model_name, results=[])

    pairs = [(request.query, doc) for doc in request.documents]
    raw_scores = manager.predict(pairs)

    scored: list[RerankResultItem] = [
        RerankResultItem(index=idx, relevance_score=score)
        for idx, score in enumerate(raw_scores)
    ]
    # Sort descending by relevance score; stable tie-break by index
    scored.sort(key=lambda item: (-item.relevance_score, item.index))

    top_n = request.top_n
    if top_n is not None and top_n >= 0:
        scored = scored[:top_n]

    return RerankResponse(model=manager.model_name, results=scored)


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    """Observed runtime metrics: peak RSS, query count, scored pairs."""
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    return {
        "model": manager.model_name,
        "ready": manager.is_ready,
        "peak_rss_mb": rss_mb,
        "torch_num_threads": torch.get_num_threads(),
        "query_count": manager.query_count,
        "total_pairs_scored": manager.total_pairs_scored,
    }
