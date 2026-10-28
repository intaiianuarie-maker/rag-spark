/**
 * M5-001a REV-2 Finding 1 — proxy forwards SSE as a live stream.
 *
 * `forward()` used to `await upstream.text()` before returning, so an Ask SSE
 * answer reached the client only after upstream EOF. This test drives the real
 * `forward()` against a CONTROLLED upstream that emits one frame then stalls:
 * the client must receive the first token BEFORE the upstream completes, and
 * the 503/403 cases must still forward headers + body unchanged.
 *
 * Same harness as `route.test.ts`: only global `fetch` + `next/headers` are
 * stubbed. The route handlers are NOT used — `forward()` is driven directly so
 * the streaming decision is pinned in isolation.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const cookieGetMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: cookieGetMock })),
}));

import { forward } from "./handlers";

const backendFetch = vi.fn<(input: RequestInfo, init?: RequestInit) => Promise<Response>>();

function askRequest(): Request {
  return new Request("http://web.local/api/backend/v1/workspaces/ws-1/ask/stream", {
    method: "POST",
    body: JSON.stringify({ query: "stream me" }),
    headers: { "content-type": "application/json" },
  });
}

/** An SSE frame with a single token. */
function tokenFrame(text: string): string {
  return `event: token\ndata: ${JSON.stringify({ text })}\n\n`;
}

/**
 * A controllable upstream: emits `frames` immediately, then stays open until
 * `release()` is called, at which point it closes the stream. This lets the
 * test prove the client received the first frame BEFORE the upstream finished.
 */
function controlledSse(frames: string[]) {
  const encoder = new TextEncoder();
  let release: () => void = () => {};
  const opened = new Promise<void>((resolve) => {
    release = resolve;
  });
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(encoder.encode(f));
      // Frames are enqueued immediately; the stream stays open until released.
      // Idempotent: a client-side cancel (which tears the pipe down) may race
      // with this close — swallow the "already closed"/"already errored" case.
      opened
        .then(() => {
          try {
            controller.close();
          } catch {
            // already closed/cancelled — the client tore down first
          }
        })
        .catch(() => undefined);
    },
    cancel() {
      // Client cancelled the body: resolve release so the close attempt above
      // is a no-op rather than an unhandled rejection.
      release();
    },
  });
  return {
    response: new Response(stream, {
      status: 200,
      headers: {
        "content-type": "text/event-stream",
        "x-accel-buffering": "no",
      },
    }),
    release,
  };
}

/** Acquire the client body reader and read ONE chunk, or nothing on timeout
   * (the stream is deliberately left open — that is the whole point of this
   * test). Returns the reader so the caller can drain the SAME body. */
  async function acquireReader(
    response: Response,
    timeoutMs = 1_000,
  ): Promise<{ reader: ReadableStreamDefaultReader<Uint8Array>; text: string } | null> {
    const reader = response.body?.getReader();
    if (!reader) return null;
    const decoder = new TextDecoder();
    const timer = setTimeout(() => {
      void reader.cancel().catch(() => undefined);
    }, timeoutMs);
    try {
      const { value, done } = await reader.read();
      if (done || !value) return null;
      return { reader, text: decoder.decode(value, { stream: true }) };
    } finally {
      clearTimeout(timer);
    }
  }

  /** Drain the rest of a body using a reader already acquired for it. */
  async function drainWith(reader: ReadableStreamDefaultReader<Uint8Array>): Promise<string> {
    const decoder = new TextDecoder();
    let out = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      out += decoder.decode(value, { stream: true });
    }
    await reader.cancel().catch(() => undefined);
    return out;
  }

  /** Drain a finite response body with a fresh reader (503/403 frames). */
  async function drain(response: Response): Promise<string> {
    const reader = response.body?.getReader();
    if (!reader) return "";
    const decoder = new TextDecoder();
    let out = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      out += decoder.decode(value, { stream: true });
    }
    await reader.cancel().catch(() => undefined);
    return out;
  }

describe("M5-001a REV-2 Finding 1 — SSE is streamed, not buffered", () => {
  beforeEach(() => {
    backendFetch.mockReset();
    vi.stubGlobal("fetch", backendFetch as unknown as typeof fetch);
    cookieGetMock.mockReturnValue({ value: "test-access-token" });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("client receives the first token BEFORE the upstream completes", async () => {
    const { response, release } = controlledSse([tokenFrame("Hello ")]);
    backendFetch.mockResolvedValueOnce(response);

    const startedAt = Date.now();
    const proxyResponse = await forward(askRequest(), ["v1", "workspaces", "ws-1", "ask", "stream"]);
    expect(proxyResponse.status).toBe(200);
    expect(proxyResponse.headers.get("content-type")).toBe("text/event-stream");
    // x-accel-buffering forwarded verbatim so a reverse proxy never re-buffers.
    expect(proxyResponse.headers.get("x-accel-buffering")).toBe("no");

    // Read the first chunk from the client side. The upstream stream is
    // deliberately left OPEN (it only closes on `release()`), so a blocking
    // read would hang forever — acquire a reader with a timeout instead and
    // drain the SAME body afterwards (one reader per body).
    const acquired = await acquireReader(proxyResponse, 1_000);
    expect(acquired).not.toBeNull();
    expect(acquired?.text).toContain("Hello ");
    const ttfb = Date.now() - startedAt;
    // The first token arrived quickly — well before the upstream closes.
    expect(ttfb).toBeLessThan(2_000);

    // Now let the upstream finish; the client stream should complete cleanly.
    release();
    const rest = acquired ? await drainWith(acquired.reader) : "";
    expect(rest).toBe("");
  });

  it("forwards a 503 SSE error frame with its code/message/trace_id", async () => {
    const frame =
      "event: error\ndata: " +
      JSON.stringify({
        error: "backend unavailable",
        code: "BACKEND_UNAVAILABLE",
        trace_id: "trace-9",
      }) +
      "\n\n";
    backendFetch.mockResolvedValueOnce(
      new Response(frame, {
        status: 503,
        headers: { "content-type": "text/event-stream" },
      }),
    );

    const proxyResponse = await forward(askRequest(), ["v1", "workspaces", "ws-1", "ask", "stream"]);
    expect(proxyResponse.status).toBe(503);
    expect(proxyResponse.headers.get("content-type")).toBe("text/event-stream");
    const text = await drain(proxyResponse);
    expect(text).toContain("BACKEND_UNAVAILABLE");
    expect(text).toContain("backend unavailable");
    expect(text).toContain("trace-9");
  });

  it("forwards a 403 SSE error frame with its code/message/trace_id", async () => {
    const frame =
      "event: error\ndata: " +
      JSON.stringify({
        error: "not allowed",
        code: "ACL_DENIED",
        trace_id: "trace-7",
      }) +
      "\n\n";
    backendFetch.mockResolvedValueOnce(
      new Response(frame, {
        status: 403,
        headers: { "content-type": "text/event-stream" },
      }),
    );

    const proxyResponse = await forward(askRequest(), ["v1", "workspaces", "ws-1", "ask", "stream"]);
    expect(proxyResponse.status).toBe(403);
    expect(proxyResponse.headers.get("content-type")).toBe("text/event-stream");
    const text = await drain(proxyResponse);
    expect(text).toContain("ACL_DENIED");
    expect(text).toContain("not allowed");
    expect(text).toContain("trace-7");
  });
});