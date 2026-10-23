/**
 * Regression tests for the /api/backend proxy forwarding edge cases
 * (M1-002b REWORK row #33, test1 report R1).
 *
 * Root cause: constructing `new Response("", { status: 204 })` throws
 * "TypeError: Invalid response status code 204" — bodiless statuses (204,
 * 205, 304) must be forwarded with a null body.
 *
 * `next/headers` is mocked for the cookie read; the global fetch is stubbed,
 * and the real `forward()` / route `DELETE()` handlers are driven.
 *
 * Extended (T-V1-012) with the raw-upload forwarding block: byte-identity at
 * three stages, multipart boundary preservation, size limit + cancellation.
 */
import { createHash } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const cookieGetMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: cookieGetMock })),
}));

import { forward, DELETE, POST, MAX_UPLOAD_BYTES, UploadTooLargeError, boundedBodyStream } from "./handlers";

const backendFetch = vi.fn<(input: RequestInfo, init?: RequestInit) => Promise<Response>>();

function respond(status: number, body: string = "", headers?: HeadersInit): Response {
  return new Response(status === 204 || status === 205 || status === 304 ? null : body, {
    status,
    headers,
  });
}

describe("/api/backend proxy forwarding", () => {
  beforeEach(() => {
    backendFetch.mockReset();
    vi.stubGlobal("fetch", backendFetch as unknown as typeof fetch);
    cookieGetMock.mockReturnValue({ value: "test-access-token" });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("forwards upstream 204 bodiless (key revoke regression, test1 R1)", async () => {
    backendFetch.mockResolvedValueOnce(respond(204));

    const response = await DELETE(
      new Request("http://web.local/api/backend/v1/workspaces/ws-1/keys/k-1", { method: "DELETE" }),
      { params: Promise.resolve({ path: ["v1", "workspaces", "ws-1", "keys", "k-1"] }) },
    );

    expect(response.status).toBe(204);
    await expect(response.text()).resolves.toBe("");
  });

  it("forwards 205/304 bodiless without constructing invalid Responses", async () => {
    for (const status of [205, 304]) {
      backendFetch.mockResolvedValueOnce(respond(status));
      const response = await forward(
        new Request("http://web.local/api/backend/x", { method: "GET" }),
        ["x"],
      );
      expect(response.status).toBe(status);
      await expect(response.text()).resolves.toBe("");
    }
  });

  it("keeps bodies + content-type for normal statuses", async () => {
    backendFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ member: "row" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const response = await forward(
      new Request("http://web.local/api/backend/v1/x", { method: "GET" }),
      ["v1", "x"],
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("application/json");
    await expect(response.json()).resolves.toEqual({ member: "row" });
  });

  it("DELETE handler forwards member-remove 204 through the real handler", async () => {
    backendFetch.mockResolvedValueOnce(respond(204));

    const response = await DELETE(
      new Request("http://web.local/api/backend/v1/workspaces/ws/members/u", { method: "DELETE" }),
      { params: Promise.resolve({ path: ["v1", "workspaces", "ws", "members", "u"] }) },
    );

    expect(response.status).toBe(204);
    await expect(response.text()).resolves.toBe("");
    expect(backendFetch).toHaveBeenCalledTimes(1);
  });

  it("rejects unsupported methods with 405 before contacting the backend", async () => {
    // NB: `new Request(..., {method:"TRACE"})` throws inside the Request
    // constructor (fetch spec), so pass a minimal request stub instead.
    const stubRequest = {
      method: "TRACE",
      url: "http://web.local/api/backend/x",
    } as unknown as Request;
    const response = await forward(stubRequest, ["x"]);

    expect(response.status).toBe(405);
    expect(backendFetch).not.toHaveBeenCalled();
  });

  it("attaches the bearer cookie as Authorization (no cookie passthrough)", async () => {
    backendFetch.mockResolvedValueOnce(respond(200, "{}"));
    await forward(new Request("http://web.local/api/backend/v1/ws", { method: "GET" }), ["v1", "ws"]);

    const [, init] = backendFetch.mock.calls[0];
    const headers = init?.headers as Record<string, string>;
    expect(headers.authorization).toBe("Bearer test-access-token");
    expect(Object.keys(headers)).not.toContain("cookie");
  });
});

/* ---------------------------------------------------------------------------
 * T-V1-012 — proxy forwards raw upload bytes (RP-07 / QF-07).
 *
 * The old path decoded bodies with `request.text()` and re-encoded the string,
 * which mutates invalid-UTF-8 binary (134→140-byte corruption). These tests
 * pin the replacement: raw/stream forwarding with byte identity proven at
 * three stages (pre-transport / at-handler / stored), multipart
 * content-type+boundary preservation, the size limit (declared AND actual),
 * defined cancellation, and the JSON path staying byte-identical
 * (T-V1-010 transport contract: never re-serialize JSON bodies).
 * ------------------------------------------------------------------------ */

const UPLOAD_PATH = ["v1", "workspaces", "ws-1", "documents"] as const;
const UPLOAD_URL = "http://web.local/api/backend/v1/workspaces/ws-1/documents";
const MULTIPART_CT = "multipart/form-data; boundary=----kios-tv1012-boundary";

/**
 * Hostile multipart fixture: invalid-UTF-8 payload (0xFF/0xFE invalid lead
 * bytes, a lone 0xC3 continuation, an overlong sequence, a NUL) that a
 * text()-round-trip would corrupt into U+FFFD replacement bytes. Boundary
 * framing mirrors a real browser FormData upload.
 */
function multipartFixture(): Buffer {
  const head = Buffer.from(
    "------kios-tv1012-boundary\r\n" +
      'Content-Disposition: form-data; name="file"; filename="hostile.bin"\r\n' +
      "Content-Type: application/octet-stream\r\n\r\n",
    "utf8",
  );
  const payload = Buffer.from([
    0x00, 0xff, 0xfe, 0xc3, 0x28, 0x80, 0xe2, 0x28, 0xa1, 0xf0, 0x9f, 0x92, 0xa9, 0x0a, 0xde, 0xad,
    0xbe, 0xef,
  ]);
  const tail = Buffer.from("\r\n------kios-tv1012-boundary--\r\n", "utf8");
  return Buffer.concat([head, payload, tail]);
}

/** Raw binary body for a non-multipart route (e.g. octet-stream PUT). */
function binaryFixture(): Buffer {
  return Buffer.from(Array.from({ length: 2048 }, (_, i) => (i * 37 + 11) % 256));
}

function sha256(data: Uint8Array): string {
  return createHash("sha256").update(data).digest("hex");
}

/** len+sha fingerprint of a stage, for 3-stage equality assertions. */
function fingerprint(data: Uint8Array): { bytes: number; sha256: string } {
  return { bytes: data.byteLength, sha256: sha256(data) };
}

function uploadRequest(
  body: BodyInit,
  contentType: string,
  extraHeaders?: Record<string, string>,
): Request {
  return new Request(UPLOAD_URL, {
    method: "POST",
    body,
    headers: { "content-type": contentType, ...extraHeaders },
  });
}

interface UploadEvidence {
  /** First chunk AS IT ARRIVED at the handler (arrival-time identity). */
  firstChunk: Uint8Array | undefined;
  /** Full byte run the handler read off the wire. */
  received: Buffer;
  /** What the handler persisted (simulated store write → read-back). */
  stored: Buffer;
}

/**
 * Stands in for the FastAPI upload handler: reads the stream chunk-by-chunk
 * (never pre-buffered), records the first chunk it saw, then "stores" the
 * run the way upload.py does (`_read_limited` → bytes → sha256 → DB write).
 * Asserts the proxy handed it a raw STREAM — a decoded/re-encoded body would
 * surface here as a string (the T-V1-012 bug class).
 */
function captureUploadHandler(evidence: UploadEvidence) {
  return async (_input: RequestInfo, init?: RequestInit): Promise<Response> => {
    expect(init?.body).toBeInstanceOf(ReadableStream);
    const reader = (init?.body as ReadableStream<Uint8Array>).getReader();
    const chunks: Uint8Array[] = [];
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (evidence.firstChunk === undefined) evidence.firstChunk = value;
      chunks.push(value);
      // Arrival-time snapshot: survives a mid-read error, so an aborted
      // upload still shows exactly which bytes reached the handler.
      evidence.received = Buffer.concat(chunks);
    }
    evidence.stored = Buffer.from(evidence.received); // store write → read-back
    return new Response(JSON.stringify({ job_id: "j-1", status: "queued" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
}

describe("T-V1-012 raw upload forwarding", () => {
  beforeEach(() => {
    backendFetch.mockReset();
    vi.stubGlobal("fetch", backendFetch as unknown as typeof fetch);
    cookieGetMock.mockReturnValue({ value: "test-access-token" });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("forwards multipart uploads byte-identical: pre-transport == at-handler == stored", async () => {
    const fixture = multipartFixture();
    const preTransport = fingerprint(fixture);
    const evidence: UploadEvidence = {
      firstChunk: undefined,
      received: Buffer.alloc(0),
      stored: Buffer.alloc(0),
    };
    backendFetch.mockImplementationOnce(captureUploadHandler(evidence));

    const response = await forward(uploadRequest(new Uint8Array(fixture), MULTIPART_CT), [
      ...UPLOAD_PATH,
    ]);

    expect(response.status).toBe(200);
    // Multipart content-type + boundary preserved verbatim (reviewer #2).
    const [, init] = backendFetch.mock.calls[0];
    expect((init?.headers as Record<string, string>)["content-type"]).toBe(MULTIPART_CT);
    // Stage 2 — at-handler: bytes arrived unmutated, first chunk is an exact
    // prefix of the fixture (arrival-time proof, not just post-read).
    expect(evidence.firstChunk).toBeDefined();
    expect(
      sha256(evidence.firstChunk as Uint8Array),
    ).toBe(sha256(fixture.subarray(0, (evidence.firstChunk as Uint8Array).byteLength)));
    expect(fingerprint(evidence.received)).toEqual(preTransport);
    // Stage 3 — stored.
    expect(fingerprint(evidence.stored)).toEqual(preTransport);
  });

  it("forwards non-multipart binary (octet-stream) byte-identical through the real POST handler", async () => {
    const fixture = binaryFixture();
    const preTransport = fingerprint(fixture);
    const evidence: UploadEvidence = {
      firstChunk: undefined,
      received: Buffer.alloc(0),
      stored: Buffer.alloc(0),
    };
    backendFetch.mockImplementationOnce(captureUploadHandler(evidence));

    const response = await POST(uploadRequest(new Uint8Array(fixture), "application/octet-stream"), {
      params: Promise.resolve({ path: [...UPLOAD_PATH] }),
    });

    expect(response.status).toBe(200);
    expect(sha256(evidence.received)).toBe(preTransport.sha256);
    expect(evidence.received.byteLength).toBe(preTransport.bytes);
    expect(fingerprint(evidence.stored)).toEqual(preTransport);
  });

  it("forwards JSON bodies byte-for-byte — no re-serialization (T-V1-010 transport contract)", async () => {
    // Exact spacing + key order from the caller; a parse→stringify hop or a
    // text() round-trip would change these bytes (e.g. unicode escaping).
    const jsonBody = '{"query":"ünïcødé ✅","filters":{"type":null,"n":1}}';
    let handlerReceived = Buffer.alloc(0);
    let forwardedContentType: string | undefined;

    backendFetch.mockImplementationOnce(async (_input, init) => {
      expect(init?.body).toBeInstanceOf(ReadableStream); // raw stream, no sniff branch
      forwardedContentType = (init?.headers as Record<string, string>)["content-type"];
      handlerReceived = Buffer.from(
        await new Response(init?.body as ReadableStream<Uint8Array>).arrayBuffer(),
      );
      return respond(200, JSON.stringify({ results: [] }), { "content-type": "application/json" });
    });

    const response = await forward(uploadRequest(jsonBody, "application/json"), [...UPLOAD_PATH]);

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ results: [] });
    expect(forwardedContentType).toBe("application/json");
    // Byte-identical at the handler AND still parseable as the same object
    // (JSON routes unaffected — the backend model sees the exact payload).
    expect(handlerReceived.toString("utf8")).toBe(jsonBody);
    expect(JSON.parse(handlerReceived.toString("utf8"))).toEqual({
      query: "ünïcødé ✅",
      filters: { type: null, n: 1 },
    });
  });

  it("rejects declared-oversize uploads (Content-Length fence) with 413 before contacting the backend", async () => {
    const request = uploadRequest(new Uint8Array(multipartFixture()), MULTIPART_CT, {
      "content-length": String(MAX_UPLOAD_BYTES + 1),
    });

    const response = await forward(request, [...UPLOAD_PATH]);

    expect(response.status).toBe(413);
    const payload = (await response.json()) as {
      ok: boolean;
      error: { code: string; message: string };
    };
    expect(payload.ok).toBe(false);
    expect(payload.error.code).toBe("upload_too_large");
    expect(backendFetch).not.toHaveBeenCalled(); // no bytes touched, backend not contacted
  });

  it("bounds the ACTUAL byte count mid-stream (absent/lying Content-Length) → 413, no partial store", async () => {
    // Stream body: no declared length, so only the bounded counter can stop
    // it. 4 MiB chunks vs the 25 MiB cap: chunk #7 (28 MiB) must never cross.
    const chunk = new Uint8Array(4 * 1024 * 1024);
    let pulls = 0;
    const source = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls += 1;
        controller.enqueue(chunk); // infinite on purpose: only the cap can stop it
      },
    });
    const request = new Request(UPLOAD_URL, {
      method: "POST",
      body: source,
      headers: { "content-type": MULTIPART_CT },
      ...({ duplex: "half" } as object),
    } as RequestInit);

    const evidence: UploadEvidence = {
      firstChunk: undefined,
      received: Buffer.alloc(0),
      stored: Buffer.alloc(0),
    };
    backendFetch.mockImplementationOnce(captureUploadHandler(evidence));

    const response = await forward(request, [...UPLOAD_PATH]);

    expect(response.status).toBe(413);
    const payload = (await response.json()) as { ok: boolean; error: { code: string } };
    expect(payload.ok).toBe(false);
    expect(payload.error.code).toBe("upload_too_large");
    // Exactly the 6 full chunks under the cap were forwarded (24 MiB); the
    // 7th (28 MiB cumulative) was cut — nothing past the limit ever crossed.
    expect(evidence.received.byteLength).toBe(6 * 4 * 1024 * 1024);
    // Handler never completed → nothing persisted (reviewer #3: no partial).
    expect(evidence.stored.byteLength).toBe(0);
    // The infinite source was torn down early — a drained pipe would pull
    // without bound.
    expect(pulls).toBeLessThan(15);
  });

  it("client cancellation aborts the upstream call and cancels the inbound body (499)", async () => {
    const abortController = new AbortController();
    let sourceCancelled = false;
    const source = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array([1, 2, 3, 4]));
        // deliberately left open — the client hangs up mid-upload
      },
      cancel() {
        sourceCancelled = true;
      },
    });
    const request = new Request(UPLOAD_URL, {
      method: "POST",
      body: source,
      headers: { "content-type": "application/octet-stream" },
      signal: abortController.signal,
      ...({ duplex: "half" } as object),
    } as RequestInit);

    let upstreamSawAbort = false;
    backendFetch.mockImplementationOnce(async (_input, init) => {
      const reader = (init?.body as ReadableStream<Uint8Array>).getReader();
      await reader.read(); // behave like a real network send: start streaming
      await new Promise<never>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          upstreamSawAbort = true;
          // emulate undici's abort teardown: the sender cancels the body
          // stream it was handed (propagates through the bounded stream to
          // the original request body).
          void reader.cancel().catch(() => undefined);
          reject(new DOMException("This operation was aborted", "AbortError"));
        });
      });
      return respond(200, "{}"); // unreachable unless aborted (test path)
    });

    const pending = forward(request, [...UPLOAD_PATH]);
    await new Promise((resolve) => setTimeout(resolve, 20)); // let the send start
    abortController.abort(); // client hangs up
    const response = await pending;
    await new Promise((resolve) => setTimeout(resolve, 20)); // let cancel propagate

    expect(response.status).toBe(499);
    const payload = (await response.json()) as { ok: boolean; error: { code: string } };
    expect(payload.ok).toBe(false);
    expect(payload.error.code).toBe("client_disconnected");
    expect(upstreamSawAbort).toBe(true); // upstream request cancelled
    expect(sourceCancelled).toBe(true); // inbound body torn down, no dangling upload
  });

  it("boundedBodyStream passes bytes through verbatim and errors exactly past the limit", async () => {
    const chunkA = new Uint8Array(600).fill(0xab);
    const chunkB = new Uint8Array(600).fill(0xcd);
    const source = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(chunkA);
        controller.enqueue(chunkB);
        controller.close();
      },
    });

    const bounded = boundedBodyStream(source, 1024);
    const reader = bounded.getReader();

    // First chunk forwarded UNCHANGED (raw passthrough, no mutation).
    const first = await reader.read();
    expect(first.done).toBe(false);
    expect(sha256(first.value as Uint8Array)).toBe(sha256(chunkA));
    // Second chunk crosses the limit: stream errors with the typed error
    // (never buffered past the cap), which forward() maps to 413.
    await expect(reader.read()).rejects.toBeInstanceOf(UploadTooLargeError);
  });

  it("boundedBodyStream propagates cancellation to the source (client-disconnect path)", async () => {
    let cancelReason: unknown = null;
    const source = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array([9]));
      },
      cancel(reason) {
        cancelReason = reason;
      },
    });

    const bounded = boundedBodyStream(source, 1024);
    await bounded.cancel("client hung up");
    // Cancel propagates asynchronously: readable → transform writable →
    // pipeTo teardown → original source.
    await new Promise((resolve) => setTimeout(resolve, 10));

    expect(cancelReason).toBe("client hung up");
  });
});
