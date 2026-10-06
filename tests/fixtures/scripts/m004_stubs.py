"""Stub backends for the frontend proxy-branch matrix (test1, landed fixture).

Provenance: `/root/.tmp/m004_stubs.py` (handoffs/M0-003-frontend-test1.md).

    port1 -> 503 + JSON   (non-2xx passthrough branch)
    port2 -> 200 + text   (non-JSON -> 502 unexpected_response)
    port3 -> 5s delay then 200 JSON (3s AbortSignal timeout -> 502 timeout)

Bound to 127.0.0.1 only. Started by `m004_proxy_branches.sh` (which owns the
lifecycle) or manually:

    python3 tests/fixtures/scripts/m004_stubs.py
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT1, PORT2, PORT3 = 5301, 5302, 5303


class Json503Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps(
            {"status": "degraded", "service": "stub", "note": "503 branch"}
        ).encode()
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


class NonJson200Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<html>not json at all</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


class SlowHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        time.sleep(5)
        body = json.dumps({"status": "ok", "service": "slow-stub"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def serve(handler: type[BaseHTTPRequestHandler], port: int) -> None:
    ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()


def main() -> None:
    for handler, port in [
        (Json503Handler, PORT1),
        (NonJson200Handler, PORT2),
        (SlowHandler, PORT3),
    ]:
        threading.Thread(target=serve, args=(handler, port), daemon=True).start()
    print(f"stubs up: 503json=:{PORT1} nonjson=:{PORT2} slow=:{PORT3}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
