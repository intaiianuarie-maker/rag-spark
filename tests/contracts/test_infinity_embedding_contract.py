"""infinity embedding adapter contract (TASK-M3-002).

Runs the shared `check_embedding_provider_contract` against
`InfinityEmbeddingProvider` pointed at a **fake HTTP server** (stdlib
`http.server` on 127.0.0.1) that mimics infinity's OpenAI-compatible
`/embeddings`. No model weights, no external service, no API keys.

Also pins the adapter-specific promises: query/document prefixes, input-order
preservation, batching, empty-batch short-circuit, and typed errors on
non-200 / bad-dimension responses.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from retrieval.adapters.infinity_embed import (
    EmbeddingProviderError,
    InfinityEmbeddingProvider,
)
from retrieval.contract_tests import check_embedding_provider_contract

pytestmark = [pytest.mark.unit, pytest.mark.contract]

DIMENSION = 1024


def _vector(text: str, dimension: int) -> list[float]:
    """Deterministic vector for a text (same text -> same vector)."""
    values: list[float] = []
    counter = 0
    while len(values) < dimension:
        digest = hashlib.sha256(f"{text}:{counter}".encode()).digest()
        values.extend(byte / 255.0 for byte in digest)
        counter += 1
    return values[:dimension]


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

        inputs = body.get("input") or []
        dimension = 3 if self.server.mode == "bad_dim" else DIMENSION
        data = [
            {"object": "embedding", "index": index, "embedding": _vector(text, dimension)}
            for index, text in enumerate(inputs)
        ]
        if self.server.reverse_order:
            data = list(reversed(data))
        self._send(200, {"object": "list", "model": body.get("model"), "data": data})

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
    reverse_order: bool


@pytest.fixture
def stub() -> Any:
    server = _StubServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    server.mode = "ok"
    server.reverse_order = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
async def provider(stub: _StubServer) -> Any:
    host, port = stub.server_address
    client = httpx.AsyncClient(timeout=5.0)
    adapter = InfinityEmbeddingProvider(base_url=f"http://{host}:{port}", client=client)
    yield adapter
    # The fixture owns the injected client; adapter.aclose() must not touch it.
    await client.aclose()


# --- shared contract --------------------------------------------------------


async def test_adapter_satisfies_embedding_contract(provider: InfinityEmbeddingProvider) -> None:
    """Call shape, dimension (1024), determinism, empty batch, invalid input."""
    await check_embedding_provider_contract(provider)


async def test_model_identity_is_bge_m3_1024(provider: InfinityEmbeddingProvider) -> None:
    assert provider.model_name == "BAAI/bge-m3"
    assert provider.dimension == 1024
    assert provider.revision  # version-pin contract (PD-002)


async def test_request_uses_openai_compatible_payload(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    await provider.embed_documents(["alpha", "beta"])
    assert len(stub.requests) == 1
    request = stub.requests[0]
    assert request["path"] == "/embeddings"
    assert request["body"]["model"] == "BAAI/bge-m3"
    assert request["body"]["input"] == ["alpha", "beta"]
    assert request["body"]["encoding_format"] == "float"


# --- prefixes ---------------------------------------------------------------


async def test_query_and_document_prefixes_are_applied(stub: _StubServer) -> None:
    host, port = stub.server_address
    adapter = InfinityEmbeddingProvider(
        base_url=f"http://{host}:{port}",
        query_prefix="query: ",
        document_prefix="passage: ",
    )
    try:
        await adapter.embed_query("salut")
        await adapter.embed_documents(["unul", "doi"])
    finally:
        await adapter.aclose()

    sent = [text for request in stub.requests for text in request["body"]["input"]]
    assert sent == ["query: salut", "passage: unul", "passage: doi"]


async def test_bge_m3_defaults_have_no_prefix(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    await provider.embed_query("salut")
    assert stub.requests[0]["body"]["input"] == ["salut"]


# --- ordering / batching ----------------------------------------------------


async def test_input_order_is_preserved_when_server_reorders(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    stub.reverse_order = True
    vectors = await provider.embed_documents(["alpha", "beta", "gamma"])
    assert vectors == [_vector(t, DIMENSION) for t in ("alpha", "beta", "gamma")]


async def test_batching_splits_requests(stub: _StubServer) -> None:
    host, port = stub.server_address
    adapter = InfinityEmbeddingProvider(base_url=f"http://{host}:{port}", batch_size=2)
    try:
        vectors = await adapter.embed_documents(["a", "b", "c", "d", "e"])
    finally:
        await adapter.aclose()
    assert len(vectors) == 5
    assert [len(r["body"]["input"]) for r in stub.requests] == [2, 2, 1]


async def test_empty_batch_makes_no_request(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    assert await provider.embed_documents([]) == []
    assert stub.requests == []


# --- negative paths ---------------------------------------------------------


async def test_non_string_item_raises_before_network(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    with pytest.raises(ValueError):
        await provider.embed_documents([123])  # type: ignore[list-item]
    assert stub.requests == []


async def test_empty_query_raises(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    with pytest.raises(ValueError):
        await provider.embed_query("")
    assert stub.requests == []


async def test_http_error_is_typed(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    stub.mode = "http_500"
    with pytest.raises(EmbeddingProviderError):
        await provider.embed_documents(["alpha"])


async def test_non_json_body_is_typed(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    stub.mode = "non_json"
    with pytest.raises(EmbeddingProviderError):
        await provider.embed_documents(["alpha"])


async def test_wrong_dimension_is_rejected(
    provider: InfinityEmbeddingProvider, stub: _StubServer
) -> None:
    stub.mode = "bad_dim"
    with pytest.raises(EmbeddingProviderError):
        await provider.embed_documents(["alpha"])


async def test_unreachable_server_is_typed() -> None:
    # Port 1 on localhost refuses connections; no server needed.
    adapter = InfinityEmbeddingProvider(base_url="http://127.0.0.1:1", timeout=1.0)
    try:
        with pytest.raises(EmbeddingProviderError):
            await adapter.embed_documents(["alpha"])
    finally:
        await adapter.aclose()


async def test_injected_client_is_not_closed_by_aclose(stub: _StubServer) -> None:
    host, port = stub.server_address
    client = httpx.AsyncClient(timeout=5.0)
    adapter = InfinityEmbeddingProvider(base_url=f"http://{host}:{port}", client=client)
    await adapter.embed_query("alpha")
    await adapter.aclose()
    assert not client.is_closed  # caller-owned lifecycle
    await client.aclose()
