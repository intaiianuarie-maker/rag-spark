"""infinity reranker adapter contract (TASK-M3-004).

Runs the shared `check_reranker_contract` against `InfinityReranker` pointed at
a **fake HTTP server** (stdlib `http.server`) that mimics infinity's
`/rerank`. No weights, no external service, no API keys.

Also pins the adapter promises: OpenAI/Jina payload shape, index->item mapping
(no invented documents, provenance preserved), input not mutated, top_k
truncation, empty short-circuit, and typed errors.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from harness_fakes import make_item
from retrieval.adapters.infinity_rerank import InfinityReranker, RerankerError
from retrieval.contract_tests import check_reranker_contract
from retrieval.interfaces import RetrievalMethod

pytestmark = [pytest.mark.unit, pytest.mark.contract]

DOCUMENTS = [
    make_item("hybrid retrieval uses dense and lexical signals", 0.9, page=1, section="Intro"),
    make_item("permissions are enforced before ranking", 0.8, page=2, section="ACL"),
    make_item("unrelated paragraph about lunch", 0.7, page=3, section="Other"),
    make_item("citations resolve to evidence ids", 0.6, page=4, section="Citations"),
]


class _Handler(BaseHTTPRequestHandler):
    server: _StubServer

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append({"path": self.path, "body": body})

        if self.server.mode == "http_500":
            self._send(500, {"error": "boom"})
            return
        if self.server.mode == "non_json":
            raw = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        documents = body.get("documents") or []
        if self.server.mode == "bad_index":
            results = [{"index": 999, "relevance_score": 1.0}]
        elif self.server.mode == "bad_score":
            results = [{"index": 0, "relevance_score": "high"}]
        else:
            # Deterministic descending scores by document index.
            results = [
                {"index": index, "relevance_score": round(1.0 - index * 0.1, 4)}
                for index in range(len(documents))
            ]
        self._send(200, {"results": results})

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        pass


class _StubServer(ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    mode: str


@pytest.fixture
def stub() -> Any:
    server = _StubServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    server.mode = "ok"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
async def adapter(stub: _StubServer) -> Any:
    host, port = stub.server_address
    client = httpx.AsyncClient(timeout=5.0)
    reranker = InfinityReranker(base_url=f"http://{host}:{port}", client=client)
    yield reranker
    await client.aclose()


async def test_adapter_satisfies_reranker_contract(adapter: InfinityReranker) -> None:
    await check_reranker_contract(
        adapter, query="retrieval ranking citations", documents=DOCUMENTS, top_k=3
    )


async def test_model_identity_is_bge_reranker_m3(adapter: InfinityReranker) -> None:
    assert adapter.model_name == "BAAI/bge-reranker-v2-m3"


async def test_request_payload_shape(adapter: InfinityReranker, stub: _StubServer) -> None:
    await adapter.rerank(query="ranking", documents=DOCUMENTS, top_k=2)
    request = stub.requests[0]
    assert request["path"] == "/rerank"
    assert request["body"]["model"] == "BAAI/bge-reranker-v2-m3"
    assert request["body"]["query"] == "ranking"
    assert request["body"]["documents"] == [item.content for item in DOCUMENTS]
    assert request["body"]["top_n"] == 2


async def test_index_maps_back_to_original_items(adapter: InfinityReranker) -> None:
    ranked = await adapter.rerank(query="ranking", documents=DOCUMENTS, top_k=None)
    assert len(ranked) == len(DOCUMENTS)
    source = {(item.document_id, item.chunk_id) for item in DOCUMENTS}
    assert {(item.document_id, item.chunk_id) for item in ranked} == source
    assert all(item.method == RetrievalMethod.RERANKED for item in ranked)
    assert all("rerank" in item.scores for item in ranked)
    assert [item.score for item in ranked] == sorted(
        (item.score for item in ranked), reverse=True
    )


async def test_input_documents_are_not_mutated(adapter: InfinityReranker) -> None:
    before = [item.model_dump() for item in DOCUMENTS]
    await adapter.rerank(query="ranking", documents=DOCUMENTS, top_k=2)
    assert [item.model_dump() for item in DOCUMENTS] == before


async def test_empty_input_short_circuits(adapter: InfinityReranker, stub: _StubServer) -> None:
    assert await adapter.rerank(query="ranking", documents=[], top_k=3) == []
    assert stub.requests == []


async def test_top_k_none_keeps_all(adapter: InfinityReranker) -> None:
    ranked = await adapter.rerank(query="ranking", documents=DOCUMENTS, top_k=None)
    assert len(ranked) == len(DOCUMENTS)


async def test_http_error_is_typed(adapter: InfinityReranker, stub: _StubServer) -> None:
    stub.mode = "http_500"
    with pytest.raises(RerankerError):
        await adapter.rerank(query="ranking", documents=DOCUMENTS, top_k=2)


async def test_non_json_body_is_typed(adapter: InfinityReranker, stub: _StubServer) -> None:
    stub.mode = "non_json"
    with pytest.raises(RerankerError):
        await adapter.rerank(query="ranking", documents=DOCUMENTS, top_k=2)


async def test_out_of_range_index_is_typed(adapter: InfinityReranker, stub: _StubServer) -> None:
    stub.mode = "bad_index"
    with pytest.raises(RerankerError):
        await adapter.rerank(query="ranking", documents=DOCUMENTS, top_k=2)


async def test_non_numeric_score_is_typed(adapter: InfinityReranker, stub: _StubServer) -> None:
    stub.mode = "bad_score"
    with pytest.raises(RerankerError):
        await adapter.rerank(query="ranking", documents=DOCUMENTS, top_k=2)


async def test_unreachable_server_is_typed() -> None:
    reranker = InfinityReranker(base_url="http://127.0.0.1:1", timeout=1.0)
    try:
        with pytest.raises(RerankerError):
            await reranker.rerank(query="ranking", documents=DOCUMENTS, top_k=2)
    finally:
        await reranker.aclose()


async def test_injected_client_is_not_closed_by_aclose(stub: _StubServer) -> None:
    host, port = stub.server_address
    client = httpx.AsyncClient(timeout=5.0)
    reranker = InfinityReranker(base_url=f"http://{host}:{port}", client=client)
    await reranker.rerank(query="ranking", documents=DOCUMENTS, top_k=1)
    await reranker.aclose()
    assert not client.is_closed
    await client.aclose()
