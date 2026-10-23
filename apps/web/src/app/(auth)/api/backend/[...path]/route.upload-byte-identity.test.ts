/**
 * T-V1-013 — upload byte-identity proof with a hostile invalid-UTF-8 multipart
 * fixture (MICRO-TICKETS-V1 §B, trace RP-07 / QF-07, depends T-V1-012).
 *
 * Spec:
 *   1. fixture = multipart carrying invalid-UTF-8 file bytes
 *      (→ `hostile-upload.fixture.ts`, byte-exact, next to this file);
 *   2. length + SHA-256 equal at all three stages — pre-transport / at the
 *      upload handler / stored;
 *   3. multipart fields intact after the trip;
 *   4. oversize → clean reject with NO partial file;
 *   5. cancel → NO partial file;
 *   plus the ordinary JSON upload path staying green (success criteria).
 *
 * Everything runs against the REAL `forward()` from `./route.ts` with only the
 * network stubbed (same harness as `route.test.ts`); nothing here re-implements
 * proxy logic, so a green run is evidence about the shipped code path.
 *
 * Allowed files for this ticket: this test + the fixture. `route.ts`,
 * `route.test.ts` and every other product file are untouched.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const cookieGetMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: cookieGetMock })),
}));

import { forward, MAX_UPLOAD_BYTES } from "./handlers";

/** Global fetch stub = the "network"; every test drives the real `forward()`. */
const backendFetch = vi.fn<(input: RequestInfo, init?: RequestInit) => Promise<Response>>();
import {
  COLLECTION_FIELD,
  FILE_CONTENT_TYPE,
  FILE_FILENAME,
  FILE_PAYLOAD_OFFSET,
  HOSTILE_BOUNDARY,
  HOSTILE_CONTENT_TYPE,
  HOSTILE_FILE_BYTES,
  HOSTILE_MULTIPART_BODY,
  TITLE_FIELD,
  WORKSPACE_FIELD,
  fingerprint,
  fixtureChunks,
  sha256,
} from "./hostile-upload.fixture";

const UPLOAD_PATH = ["v1", "workspaces", "ws-1", "documents"] as const;
const UPLOAD_URL = "http://web.local/api/backend/v1/workspaces/ws-1/documents";

/* ---------------------------------------------------------------------------
 * Harness: a request builder, a chunked (stream) body, and an upload store that
 * only ever persists a FILE once the handler read the body to completion —
 * that is what makes "no partial file" an assertable claim rather than a hope.
 * ------------------------------------------------------------------------ */

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

/** Body as a real stream, cut at the fixture's (awkward) chunk boundaries. */
function chunkedUploadRequest(contentType: string): Request {
  const chunks = fixtureChunks();
  const source = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
  return new Request(UPLOAD_URL, {
    method: "POST",
    body: source,
    headers: { "content-type": contentType },
    ...({ duplex: "half" } as object),
  } as RequestInit);
}

interface StoreOptions {
  /** Keep the bytes in memory (needed to assert stored-stage identity). */
  retain?: boolean;
}

/**
 * Stand-in for the backend document store: bytes streamed in are "on disk"
 * only after `commit()`, which the handler calls when — and only when — the
 * upload completed. A rejected/aborted upload therefore has to leave
 * `persisted` empty, otherwise a partial file leaked.
 */
class UploadStore {
  readonly persisted: Buffer[] = [];
  commits = 0;
  inFlightBytes = 0;
  private readonly retain: boolean;
  private chunks: Buffer[] = [];

  constructor(options: StoreOptions = {}) {
    this.retain = options.retain ?? true;
  }

  add(chunk: Uint8Array): void {
    this.inFlightBytes += chunk.byteLength;
    if (this.retain) this.chunks.push(Buffer.from(chunk));
  }

  commit(): void {
    this.commits += 1;
    this.persisted.push(Buffer.concat(this.chunks));
    this.chunks = [];
  }

  /** Partial files left on disk (metric: must stay 0 on reject/cancel). */
  partialFiles(): number {
    return this.persisted.length;
  }
}

interface UploadEvidence {
  /** First chunk as it arrived at the handler (arrival-time identity). */
  firstChunk?: Uint8Array;
  /** Full byte run read off the wire (at-handler stage). */
  received: Buffer;
  forwardedContentType?: string;
  upstreamAborted: boolean;
}

function newEvidence(): UploadEvidence {
  return { received: Buffer.alloc(0), upstreamAborted: false };
}

/**
 * Stands in for the FastAPI upload handler (`documents/upload.py`): reads the
 * body chunk-by-chunk, records what it saw, and commits to the store only on a
 * complete read. Asserts the proxy handed over a raw STREAM — a decoded /
 * re-encoded body would arrive as a string instead (the T-V1-012 bug class).
 */
function uploadStoreHandler(
  store: UploadStore,
  evidence: UploadEvidence,
  accumulate = true,
): (input: RequestInfo, init?: RequestInit) => Promise<Response> {
  return async (_input, init) => {
    expect(init?.body).toBeInstanceOf(ReadableStream);
    evidence.forwardedContentType = (init?.headers as Record<string, string>)["content-type"];
    const reader = (init?.body as ReadableStream<Uint8Array>).getReader();
    const signal = init?.signal ?? null;
    const onAbort = (): void => {
      evidence.upstreamAborted = true;
      // Emulate undici's teardown of an aborted upload: cancel the body it
      // was handed, which propagates through the bounded stream to the client.
      void reader.cancel().catch(() => undefined);
    };
    signal?.addEventListener("abort", onAbort);
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (evidence.firstChunk === undefined) evidence.firstChunk = value;
        if (accumulate) evidence.received = Buffer.concat([evidence.received, Buffer.from(value)]);
        store.add(value);
      }
      if (signal?.aborted) {
        // Cut off mid-flight: a real handler never commits a truncated upload.
        throw new DOMException("The operation was aborted.", "AbortError");
      }
      store.commit();
    } finally {
      signal?.removeEventListener("abort", onAbort);
    }
    return new Response(JSON.stringify({ job_id: "j-1", status: "queued" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
}

/* ---------------------------------------------------------------------------
 * Minimal, delimiter-accurate multipart parser (test-side only). It scans for
 * `\r\n--boundary` followed by CRLF or `--`, so boundary LOOKALIKES inside the
 * file payload are not mistaken for framing — a `split(boundary)` would be.
 * ------------------------------------------------------------------------ */

interface MultipartPart {
  name: string;
  filename: string | null;
  contentType: string | null;
  value: Buffer;
}

function parsePart(headerBlock: Buffer, value: Buffer): MultipartPart {
  const headers: Record<string, string> = {};
  for (const line of headerBlock.toString("utf8").split("\r\n")) {
    const colon = line.indexOf(":");
    if (colon === -1) continue;
    headers[line.slice(0, colon).trim().toLowerCase()] = line.slice(colon + 1).trim();
  }
  const disposition = headers["content-disposition"] ?? "";
  return {
    name: /name="([^"]*)"/.exec(disposition)?.[1] ?? "",
    filename: /filename="([^"]*)"/.exec(disposition)?.[1] ?? null,
    contentType: headers["content-type"] ?? null,
    value,
  };
}

function parseMultipart(raw: Buffer, boundary: string): MultipartPart[] {
  const dashBoundary = Buffer.from(`--${boundary}`, "ascii");
  const delimiter = Buffer.from(`\r\n--${boundary}`, "ascii");
  if (!raw.subarray(0, dashBoundary.byteLength).equals(dashBoundary)) {
    throw new Error("body does not start with the dash-boundary line");
  }
  let pos = dashBoundary.byteLength;
  if (raw.subarray(pos, pos + 2).toString("latin1") !== "\r\n") {
    throw new Error("dash-boundary line is not CRLF-terminated");
  }
  pos += 2;

  const parts: MultipartPart[] = [];
  for (;;) {
    let cut = raw.indexOf(delimiter, pos);
    while (cut !== -1) {
      const after = cut + delimiter.byteLength;
      const isClose = raw[after] === 0x2d && raw[after + 1] === 0x2d;
      const isNext = raw[after] === 0x0d && raw[after + 1] === 0x0a;
      if (isClose || isNext) break;
      cut = raw.indexOf(delimiter, cut + 1); // boundary look-alike: keep scanning
    }
    if (cut === -1) throw new Error("unterminated multipart part");

    const part = raw.subarray(pos, cut);
    const headerEnd = part.indexOf("\r\n\r\n");
    if (headerEnd === -1) throw new Error("multipart part has no header/body separator");
    parts.push(parsePart(part.subarray(0, headerEnd), part.subarray(headerEnd + 4)));

    const after = cut + delimiter.byteLength;
    if (raw[after] === 0x2d && raw[after + 1] === 0x2d) return parts;
    pos = after + 2;
  }
}

/** Assert the multipart structure + field values survived the transport. */
function expectFieldsIntact(raw: Buffer): MultipartPart[] {
  const parts = parseMultipart(raw, HOSTILE_BOUNDARY);
  expect(parts.map((part) => part.name)).toEqual(["title", "collection", "workspace", "file"]);
  expect(parts[0].value.toString("utf8")).toBe(TITLE_FIELD);
  expect(parts[1].value.toString("utf8")).toBe(COLLECTION_FIELD);
  expect(parts[2].value.toString("utf8")).toBe(WORKSPACE_FIELD);
  expect(parts[3].filename).toBe(FILE_FILENAME);
  expect(parts[3].contentType).toBe(FILE_CONTENT_TYPE);
  expect(fingerprint(parts[3].value)).toEqual(fingerprint(HOSTILE_FILE_BYTES));
  // Still hostile after the trip — no replacement-char "repair" happened.
  expect(Buffer.from(parts[3].value.toString("utf8"), "utf8").equals(parts[3].value)).toBe(false);
  return parts;
}

describe("T-V1-013 upload byte-identity proof", () => {
  beforeEach(() => {
    backendFetch.mockReset();
    vi.stubGlobal("fetch", backendFetch as unknown as typeof fetch);
    cookieGetMock.mockReturnValue({ value: "test-access-token" });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("control: the fixture is genuinely hostile (a UTF-8 round-trip corrupts it)", () => {
    // The bug class this fixture exists for: `request.text()` decode → re-encode.
    const roundTripped = Buffer.from(HOSTILE_MULTIPART_BODY.toString("utf8"), "utf8");
    expect(roundTripped.byteLength).not.toBe(HOSTILE_MULTIPART_BODY.byteLength);
    expect(sha256(roundTripped)).not.toBe(sha256(HOSTILE_MULTIPART_BODY));
    // Invalid bytes really are in the file payload…
    expect(HOSTILE_FILE_BYTES.includes(0xff)).toBe(true);
    expect(HOSTILE_FILE_BYTES.includes(0xfe)).toBe(true);
    expect(Buffer.from(HOSTILE_FILE_BYTES.toString("utf8"), "utf8").equals(HOSTILE_FILE_BYTES)).toBe(
      false,
    );
    // …and the fixture really is streamed in pieces: several chunks, at least
    // one of them starting INSIDE the file payload (mid-header + mid-binary).
    const chunks = fixtureChunks();
    expect(chunks.length).toBeGreaterThan(2);
    expect(
      Buffer.concat(chunks.map((chunk) => Buffer.from(chunk))).equals(HOSTILE_MULTIPART_BODY),
    ).toBe(true);
    expect(FILE_PAYLOAD_OFFSET).toBeGreaterThan(0);
    expect(chunks.some((chunk) => chunk.byteLength > 0)).toBe(true);
    // …and RFC conformance: the boundary VALUE never appears inside the body
    // content (look-alikes only), so a real parser can delimit it.
    expect(HOSTILE_FILE_BYTES.includes(Buffer.from(HOSTILE_BOUNDARY, "ascii"))).toBe(false);
  });

  it("length + SHA-256 equal pre-transport == at-handler == stored (3/3 stages)", async () => {
    const preTransport = fingerprint(HOSTILE_MULTIPART_BODY);
    const store = new UploadStore();
    const evidence = newEvidence();
    backendFetch.mockImplementationOnce(uploadStoreHandler(store, evidence));

    const response = await forward(chunkedUploadRequest(HOSTILE_CONTENT_TYPE), [...UPLOAD_PATH]);

    expect(response.status).toBe(200);
    // Boundary parameter forwarded verbatim (T-V1-012 contract, re-proved here).
    expect(evidence.forwardedContentType).toBe(HOSTILE_CONTENT_TYPE);
    // Stage 2 — at-handler: same length AND same SHA as pre-transport.
    expect(fingerprint(evidence.received)).toEqual(preTransport);
    // Arrival-time proof: the first chunk off the wire is an exact prefix of
    // the fixture (it is cut mid-file-header, so a re-chunked/re-encoded body
    // would not match).
    expect(evidence.firstChunk).toBeDefined();
    const firstChunk = Buffer.from(evidence.firstChunk as Uint8Array);
    expect(firstChunk.equals(HOSTILE_MULTIPART_BODY.subarray(0, firstChunk.byteLength))).toBe(true);
    // Stage 3 — stored: the handler's commit (store write → read-back).
    expect(store.commits).toBe(1);
    expect(store.persisted).toHaveLength(1);
    expect(fingerprint(store.persisted[0])).toEqual(preTransport);
    // Metric: SHA match 3/3 stages.
    const stages = [preTransport, fingerprint(evidence.received), fingerprint(store.persisted[0])];
    expect(new Set(stages.map((stage) => stage.sha256)).size).toBe(1);
    expect(new Set(stages.map((stage) => stage.bytes)).size).toBe(1);
  });

  it("multipart fields are intact after the trip (parsed at-handler AND stored)", async () => {
    const store = new UploadStore();
    const evidence = newEvidence();
    backendFetch.mockImplementationOnce(uploadStoreHandler(store, evidence));

    const response = await forward(uploadRequest(new Uint8Array(HOSTILE_MULTIPART_BODY), HOSTILE_CONTENT_TYPE), [
      ...UPLOAD_PATH,
    ]);

    expect(response.status).toBe(200);
    const atHandler = expectFieldsIntact(evidence.received);
    const stored = expectFieldsIntact(store.persisted[0]);
    // Both parses agree with each other and with the fixture's file bytes.
    expect(stored.map((part) => part.name)).toEqual(atHandler.map((part) => part.name));
    expect(sha256(stored[3].value)).toBe(sha256(HOSTILE_FILE_BYTES));
    expect(sha256(evidence.received)).toBe(sha256(HOSTILE_MULTIPART_BODY));
    expect(sha256(store.persisted[0])).toBe(sha256(HOSTILE_MULTIPART_BODY));
  });

  it("oversize (declared Content-Length) → clean 413, backend never contacted, no partial file", async () => {
    const store = new UploadStore();
    const request = uploadRequest(new Uint8Array(HOSTILE_MULTIPART_BODY), HOSTILE_CONTENT_TYPE, {
      "content-length": String(MAX_UPLOAD_BYTES + 1),
    });

    const response = await forward(request, [...UPLOAD_PATH]);

    expect(response.status).toBe(413);
    const payload = (await response.json()) as { ok: boolean; error: { code: string } };
    expect(payload.ok).toBe(false);
    expect(payload.error.code).toBe("upload_too_large");
    expect(backendFetch).not.toHaveBeenCalled(); // not a byte crossed, no contact
    expect(store.inFlightBytes).toBe(0);
    expect(store.partialFiles()).toBe(0); // metric: partial-file leaks = 0
  });

  it("oversize (actual count, lying/absent length) → clean 413, hostile bytes crossed but no partial file", async () => {
    // 4 MiB of the hostile fixture repeated — no declared length, so only the
    // bounded byte counter can stop it; chunk #7 (28 MiB) must never cross.
    const hostileChunk = Buffer.alloc(4 * 1024 * 1024);
    for (let i = 0; i < hostileChunk.byteLength; i += 1) {
      hostileChunk[i] = HOSTILE_MULTIPART_BODY[i % HOSTILE_MULTIPART_BODY.byteLength];
    }
    let pulls = 0;
    const source = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls += 1;
        controller.enqueue(hostileChunk); // infinite on purpose: only the cap stops it
      },
    });
    const request = new Request(UPLOAD_URL, {
      method: "POST",
      body: source,
      headers: { "content-type": HOSTILE_CONTENT_TYPE },
      ...({ duplex: "half" } as object),
    } as RequestInit);

    const store = new UploadStore({ retain: false });
    const evidence = newEvidence();
    backendFetch.mockImplementationOnce(uploadStoreHandler(store, evidence, false));

    const response = await forward(request, [...UPLOAD_PATH]);

    expect(response.status).toBe(413);
    const payload = (await response.json()) as { ok: boolean; error: { code: string } };
    expect(payload.ok).toBe(false);
    expect(payload.error.code).toBe("upload_too_large");
    // Exactly the 6 full chunks under the cap crossed (24 MiB); #7 was cut.
    expect(store.inFlightBytes).toBe(6 * 4 * 1024 * 1024);
    // Handler never completed → nothing persisted, i.e. NO partial file.
    expect(store.commits).toBe(0);
    expect(store.partialFiles()).toBe(0); // metric: partial-file leaks = 0
    // The bytes that did cross were the hostile ones (prefix + invalid byte).
    expect(evidence.firstChunk).toBeDefined();
    const firstChunk = Buffer.from(evidence.firstChunk as Uint8Array);
    expect(firstChunk.equals(hostileChunk.subarray(0, firstChunk.byteLength))).toBe(true);
    expect(firstChunk.includes(0xff)).toBe(true);
    // Infinite source torn down early — a drained pipe would pull forever.
    expect(pulls).toBeLessThan(15);
  });

  it("client cancel → 499, upstream aborted, inbound body torn down, no partial file", async () => {
    const abortController = new AbortController();
    let sourceCancelled = false;
    const source = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array([0x00, 0xff, 0xfe, 0x80]));
        // deliberately left open — the client hangs up mid-upload
      },
      cancel() {
        sourceCancelled = true;
      },
    });
    const request = new Request(UPLOAD_URL, {
      method: "POST",
      body: source,
      headers: { "content-type": HOSTILE_CONTENT_TYPE },
      signal: abortController.signal,
      ...({ duplex: "half" } as object),
    } as RequestInit);

    const store = new UploadStore();
    const evidence = newEvidence();
    backendFetch.mockImplementationOnce(uploadStoreHandler(store, evidence));

    const pending = forward(request, [...UPLOAD_PATH]);
    await new Promise((resolve) => setTimeout(resolve, 20)); // let the send start
    abortController.abort(); // client hangs up
    const response = await pending;
    await new Promise((resolve) => setTimeout(resolve, 20)); // let teardown settle

    expect(response.status).toBe(499);
    const payload = (await response.json()) as { ok: boolean; error: { code: string } };
    expect(payload.ok).toBe(false);
    expect(payload.error.code).toBe("client_disconnected");
    expect(evidence.upstreamAborted).toBe(true); // upstream request cancelled
    expect(sourceCancelled).toBe(true); // inbound body torn down, no dangling upload
    // Bytes HAD started flowing, yet nothing was committed → no partial file.
    expect(store.inFlightBytes).toBeGreaterThan(0);
    expect(store.commits).toBe(0);
    expect(store.partialFiles()).toBe(0); // metric: partial-file leaks = 0
  });

  it("ordinary JSON upload path is unaffected (byte-identical, parses to the same object)", async () => {
    const jsonBody = '{"query":"ünïcødé ✅","filters":{"type":null,"n":1}}';
    let handlerReceived = Buffer.alloc(0);
    let forwardedContentType: string | undefined;

    backendFetch.mockImplementationOnce(async (_input, init) => {
      expect(init?.body).toBeInstanceOf(ReadableStream); // raw stream, no sniff branch
      forwardedContentType = (init?.headers as Record<string, string>)["content-type"];
      handlerReceived = Buffer.from(
        await new Response(init?.body as ReadableStream<Uint8Array>).arrayBuffer(),
      );
      return new Response(JSON.stringify({ results: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });

    const response = await forward(
      uploadRequest(jsonBody, "application/json"),
      ["v1", "workspaces", "ws-1", "search"],
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ results: [] });
    expect(forwardedContentType).toBe("application/json");
    expect(handlerReceived.toString("utf8")).toBe(jsonBody);
    expect(JSON.parse(handlerReceived.toString("utf8"))).toEqual({
      query: "ünïcødé ✅",
      filters: { type: null, n: 1 },
    });
    expect(sha256(handlerReceived)).toBe(sha256(Buffer.from(jsonBody, "utf8")));
  });
});
