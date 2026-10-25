"""Additional real-HTTP-stub integration tests for M7-001a."""

from __future__ import annotations

import http.server
import json
import os
import socket
import threading

import httpx
import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")

from models.interfaces import GenerationRequest, MessageRole
from models.providers.openai_compat import OpenAICompatProvider

pytestmark = pytest.mark.unit


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        if self.path.endswith("/chat/completions"):
            body = json.loads(raw or b"{}")
            payload = {
                "model": body.get("model"),
                "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.endswith("/embeddings"):
            body = json.loads(raw or b"{}")
            n = len(body.get("input", []))
            payload = {"data": [{"embedding": [0.5]} for _ in range(n)]}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        return


@pytest.fixture()
def stub_server():
    port = _free_port()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/v1"
    httpd.shutdown()
    thread.join(timeout=5)


def test_real_http_stub_generate_and_embed(stub_server) -> None:
    import asyncio

    p = OpenAICompatProvider(
        name="real-stub",
        base_url=stub_server,
        model="stub-model",
        embedding_dimension=1,
    )
    req = GenerationRequest(
        model="stub-model",
        messages=[
            __import__("models.interfaces", fromlist=["ChatMessage"]).ChatMessage(
                role=MessageRole.USER, content="ping"
            )
        ],
    )
    result = asyncio.run(p.generate(req))
    assert result.text == "pong"

    docs = asyncio.run(p.embed_documents(["a", "b"]))
    assert docs == [[0.5], [0.5]]
    q = asyncio.run(p.embed_query("a"))
    assert q == [0.5]


def test_real_http_stub_error_reply(stub_server) -> None:
    # hit an unknown path via direct client to ensure 404 propagates
    import asyncio

    async def run():
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{stub_server}/nope", json={})
            r.raise_for_status()

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
