"""Unit tests for standalone MiniLM rerank sidecar (T-ARCH-06).

Covers:
1. Sidecar FastAPI app routes (/health, /ready, /rerank, /metrics).
2. Readiness-as-capability contract (503 on unready, 200 on operable).
3. MiniLMReranker client adapter compliance with Reranker ABC and contract tests.
4. Torch thread clamping (1-2 threads, CPU-only).
5. Compose service block memory limit (mem_limit 1g HARD).
6. Weight purity (0 BGE weights).
7. Rerank ranking precision on discriminative probes.
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

# Ensure repo packages on sys.path
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
for _p in ("packages", "apps/api", "scripts", "."):
    _path_str = str((_REPO_ROOT / _p).resolve())
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from retrieval.contract_tests import check_reranker_contract  # noqa: E402
from retrieval.interfaces import RetrievedItem  # noqa: E402

from apps.rerank.adapter import MiniLMReranker  # noqa: E402
from apps.rerank.app.main import app, manager  # noqa: E402
from apps.rerank.proof_gate import (  # noqa: E402
    FROZEN_PROBES,
    check_no_bge_weights,
    run_proof_battery,
)


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Test client for rerank sidecar app with pre-loaded model."""
    manager.load()
    with TestClient(app) as test_client:
        yield test_client


def test_sidecar_health_and_ready_routes(client: TestClient) -> None:
    """Verify /health and /ready endpoints."""
    # Liveness
    resp_health = client.get("/health")
    assert resp_health.status_code == 200
    assert resp_health.json() == {"status": "ok"}

    # Readiness-as-capability
    resp_ready = client.get("/ready")
    assert resp_ready.status_code == 200
    data = resp_ready.json()
    assert data["ready"] is True
    assert data["model"] == "cross-encoder/ms-marco-MiniLM-L6-v2"
    assert data["device"] == "cpu"
    assert data["torch_num_threads"] in (1, 2)
    assert data["peak_rss_mb"] > 0


def test_readiness_as_capability_failure_mode(client: TestClient) -> None:
    """Verify that unready model returns 503, never false 200."""
    orig_model = manager.model
    orig_error = manager.load_error
    try:
        manager.model = None
        manager.load_error = "simulated_model_failure"

        resp_ready = client.get("/ready")
        assert resp_ready.status_code == 503
        data = resp_ready.json()
        assert data["detail"]["ready"] is False
        assert "simulated_model_failure" in data["detail"]["error"]

        # /rerank must also return 503 when unready
        resp_rerank = client.post(
            "/rerank",
            json={"query": "test", "documents": ["doc1", "doc2"]},
        )
        assert resp_rerank.status_code == 503
    finally:
        manager.model = orig_model
        manager.load_error = orig_error


def test_sidecar_rerank_scoring_and_truncation(client: TestClient) -> None:
    """Verify /rerank scores pairs accurately and respects top_n."""
    query = "What is the capital of France?"
    documents = [
        "Berlin is the capital of Germany.",
        "Paris is the capital of France.",
        "Bananas are rich in potassium.",
    ]

    # Full rerank
    resp = client.post("/rerank", json={"query": query, "documents": documents})
    assert resp.status_code == 200
    res = resp.json()
    assert res["model"] == "cross-encoder/ms-marco-MiniLM-L6-v2"
    results = res["results"]
    assert len(results) == 3

    # Paris (index 1) must be ranked 1st
    assert results[0]["index"] == 1
    assert results[0]["relevance_score"] > results[1]["relevance_score"]
    assert results[1]["relevance_score"] > results[2]["relevance_score"]

    # Top-1 truncation
    resp_top1 = client.post("/rerank", json={"query": query, "documents": documents, "top_n": 1})
    assert resp_top1.status_code == 200
    res_top1 = resp_top1.json()["results"]
    assert len(res_top1) == 1
    assert res_top1[0]["index"] == 1

    # Empty documents short-circuit
    resp_empty = client.post("/rerank", json={"query": query, "documents": []})
    assert resp_empty.status_code == 200
    assert resp_empty.json()["results"] == []


def test_metrics_route(client: TestClient) -> None:
    """Verify /metrics reports RSS and execution stats."""
    resp = client.get("/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "peak_rss_mb" in data
    assert data["peak_rss_mb"] < 1024  # Less than 1GB
    assert data["torch_num_threads"] in (1, 2)
    assert data["ready"] is True


@pytest.mark.asyncio
async def test_minilm_reranker_satisfies_contract() -> None:
    """Verify MiniLMReranker satisfies the standard Reranker ABC contract."""
    reranker = MiniLMReranker()
    test_docs = [
        RetrievedItem(
            content="Paris is the capital of France.",
            score=0.5,
            document_id="d1",
            chunk_id="c1",
        ),
        RetrievedItem(
            content="Berlin is the capital of Germany.",
            score=0.4,
            document_id="d2",
            chunk_id="c2",
        ),
        RetrievedItem(
            content="Bananas are rich in potassium.",
            score=0.3,
            document_id="d3",
            chunk_id="c3",
        ),
    ]

    await check_reranker_contract(
        reranker,
        query="What is the capital of France?",
        documents=test_docs,
        top_k=2,
    )
    await reranker.aclose()


def test_compose_file_specifies_hard_1g_limit_and_threads() -> None:
    """Verify docker-compose.yml enforces mem_limit: 1g and TORCH_NUM_THREADS."""
    compose_path = _REPO_ROOT / "docker-compose.yml"
    content = compose_path.read_text(encoding="utf-8")

    assert "rerank:" in content
    assert "mem_limit: 1g" in content
    thread_matches = (
        'TORCH_NUM_THREADS: "1"' in content
        or "TORCH_NUM_THREADS: '1'" in content
        or "TORCH_NUM_THREADS: 1" in content
    )
    assert thread_matches
    assert "http://localhost:7998/ready" in content
    assert "cross-encoder/ms-marco-MiniLM-L6-v2" in content


def test_no_bge_weights_detected() -> None:
    """Verify purity: zero BGE weights anywhere in HF hub cache."""
    assert check_no_bge_weights() is True


@pytest.mark.asyncio
async def test_rerank_proof_gate_beats_dense_baseline() -> None:
    """Execute proof gate: prove reranker wins over dense-only baseline on frozen probes."""
    results = await run_proof_battery()
    assert results["n_probes"] == len(FROZEN_PROBES)
    assert results["rerank_wins"] is True, "Proof gate failed: reranker did not win!"
    assert results["r1_gain"] >= 0.0, f"Recall@1 delta negative: {results['r1_gain']}"
    assert results["mrr_gain"] >= 0.0, f"MRR delta negative: {results['mrr_gain']}"
    assert results["peak_rss_mb"] < 1024.0, f"Peak RSS exceeded 1GiB: {results['peak_rss_mb']} MB"
