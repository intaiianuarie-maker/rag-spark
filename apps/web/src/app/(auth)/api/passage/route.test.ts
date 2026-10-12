/**
 * Tests for the single API passage-fetch route (T-V1-032 — RP-13/QF-13).
 *
 * The two properties under review:
 *  (a) the fetch is authenticated and forwarded to the backend's canonical
 *      read with the caller's bearer token (ACL stays server-side);
 *  (b) the dependency on T-V1-022's canonical source is handled EXPLICITLY —
 *      an upstream 404 becomes `passage_unavailable`, never a generic 500 and
 *      never another citation's passage.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const cookieGetMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: cookieGetMock })),
}));

import {
  GET,
  PASSAGE_TIMEOUT_MS,
  buildPassageUpstreamUrl,
  normalizePassage,
  readPassageParams,
} from "./route";

const backendFetch = vi.fn<(input: RequestInfo, init?: RequestInit) => Promise<Response>>();

function passageRequest(query: string, init?: RequestInit): Request {
  return new Request(`http://web.local/api/passage${query}`, { method: "GET", ...init });
}

beforeEach(() => {
  backendFetch.mockReset();
  vi.stubGlobal("fetch", backendFetch as unknown as typeof fetch);
  vi.stubEnv("API_BASE_URL", "http://api.test:8000");
  cookieGetMock.mockReturnValue({ value: "test-access-token" });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("readPassageParams", () => {
  it("requires both refs", () => {
    expect(readPassageParams(new URLSearchParams(""))).toMatchObject({ ok: false });
    expect(readPassageParams(new URLSearchParams("workspace_id=ws-1"))).toMatchObject({ ok: false });
    expect(readPassageParams(new URLSearchParams("chunk_id=c-1"))).toMatchObject({ ok: false });
  });

  it("accepts normal ids and rejects path traversal", () => {
    expect(readPassageParams(new URLSearchParams("workspace_id=ws-1&chunk_id=c-1"))).toEqual({
      ok: true,
      workspaceId: "ws-1",
      chunkId: "c-1",
    });
    expect(readPassageParams(new URLSearchParams("workspace_id=../etc&chunk_id=c-1"))).toMatchObject({
      ok: false,
    });
    expect(readPassageParams(new URLSearchParams("workspace_id=ws-1&chunk_id=c%2F1"))).toMatchObject({
      ok: false,
    });
  });
});

describe("buildPassageUpstreamUrl", () => {
  it("percent-encodes the refs", () => {
    expect(buildPassageUpstreamUrl("http://api.test:8000", "ws-1", "c 1")).toBe(
      "http://api.test:8000/v1/workspaces/ws-1/chunks/c%201",
    );
  });
});

describe("GET /api/passage", () => {
  it("rejects missing refs with 400 invalid_request (no upstream call)", async () => {
    const response = await GET(passageRequest("?workspace_id=ws-1"));
    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toEqual({
      ok: false,
      error: { code: "invalid_request", message: expect.any(String) },
    });
    expect(backendFetch).not.toHaveBeenCalled();
  });

  it("rejects an anonymous caller with 401 (no upstream call)", async () => {
    cookieGetMock.mockReturnValue(undefined);
    const response = await GET(passageRequest("?workspace_id=ws-1&chunk_id=c-1"));
    expect(response.status).toBe(401);
    await expect(response.json()).resolves.toMatchObject({
      error: { code: "unauthorized" },
    });
    expect(backendFetch).not.toHaveBeenCalled();
  });

  it("forwards the bearer token to the canonical read and normalizes 200", async () => {
    backendFetch.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          chunk_id: "c-1",
          content: "canonical passage text",
          document_id: "d-1",
          filename: "alpha.pdf",
          page: 3,
          section: "Results",
          span: [4, 9],
          bbox: [10, 20, 30, 40],
          workspace_id: "ws-1",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    const response = await GET(passageRequest("?workspace_id=ws-1&chunk_id=c-1"));
    expect(response.status).toBe(200);

    const [url, init] = backendFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://api.test:8000/v1/workspaces/ws-1/chunks/c-1");
    expect(init.method).toBe("GET");
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer test-access-token");
    expect(init.body).toBeUndefined();

    await expect(response.json()).resolves.toEqual({
      ok: true,
      passage: {
        chunk_id: "c-1",
        content: "canonical passage text",
        document_id: "d-1",
        filename: "alpha.pdf",
        page: 3,
        section: "Results",
        span: [4, 9],
        bbox: [10, 20, 30, 40],
        workspace_id: "ws-1",
      },
    });
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(PASSAGE_TIMEOUT_MS).toBeGreaterThan(0);
  });

  it("maps an upstream 404 to an EXPLICIT passage_unavailable (T-V1-022 pending)", async () => {
    backendFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Not Found" }), { status: 404 }),
    );

    const response = await GET(passageRequest("?workspace_id=ws-1&chunk_id=c-1"));
    expect(response.status).toBe(404);
    const body = await response.json();
    expect(body.ok).toBe(false);
    expect(body.error.code).toBe("passage_unavailable");
    expect(body.error.message).toContain("T-V1-022");
  });

  it("maps upstream 401 to 401 (the client's refresh signal)", async () => {
    backendFetch.mockResolvedValueOnce(new Response("{}", { status: 401 }));
    const response = await GET(passageRequest("?workspace_id=ws-1&chunk_id=c-1"));
    expect(response.status).toBe(401);
    await expect(response.json()).resolves.toMatchObject({ error: { code: "unauthorized" } });
  });

  it("maps upstream 403 to an explicit forbidden (ACL denial, not a 500)", async () => {
    backendFetch.mockResolvedValueOnce(new Response("{}", { status: 403 }));
    const response = await GET(passageRequest("?workspace_id=ws-1&chunk_id=c-1"));
    expect(response.status).toBe(403);
    await expect(response.json()).resolves.toMatchObject({ error: { code: "forbidden" } });
  });

  it("maps upstream 5xx to 502 backend_unreachable", async () => {
    backendFetch.mockResolvedValueOnce(new Response("boom", { status: 503 }));
    const response = await GET(passageRequest("?workspace_id=ws-1&chunk_id=c-1"));
    expect(response.status).toBe(502);
    await expect(response.json()).resolves.toMatchObject({
      error: { code: "backend_unreachable" },
    });
  });

  it("maps a network failure to 502 backend_unreachable", async () => {
    backendFetch.mockRejectedValueOnce(new TypeError("fetch failed"));
    const response = await GET(passageRequest("?workspace_id=ws-1&chunk_id=c-1"));
    expect(response.status).toBe(502);
    await expect(response.json()).resolves.toMatchObject({
      error: { code: "backend_unreachable", message: expect.stringContaining("unreachable") },
    });
  });

  it("answers 499 when the client hung up first", async () => {
    backendFetch.mockImplementationOnce(async () => {
      throw new TypeError("aborted");
    });
    const response = await GET(
      passageRequest("?workspace_id=ws-1&chunk_id=c-1", { signal: AbortSignal.abort() }),
    );
    expect(response.status).toBe(499);
    await expect(response.json()).resolves.toMatchObject({ error: { code: "client_disconnected" } });
  });

  it("maps a non-JSON 200 to 502 passage_malformed", async () => {
    backendFetch.mockResolvedValueOnce(
      new Response("<html>not json</html>", {
        status: 200,
        headers: { "content-type": "text/html" },
      }),
    );
    const response = await GET(passageRequest("?workspace_id=ws-1&chunk_id=c-1"));
    expect(response.status).toBe(502);
    await expect(response.json()).resolves.toMatchObject({ error: { code: "passage_malformed" } });
  });

  it("maps a 200 without passage text to 502 passage_malformed", async () => {
    backendFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ chunk_id: "c-1" }), { status: 200 }),
    );
    const response = await GET(passageRequest("?workspace_id=ws-1&chunk_id=c-1"));
    expect(response.status).toBe(502);
    await expect(response.json()).resolves.toMatchObject({ error: { code: "passage_malformed" } });
  });

  it("keeps the backend's own reason for a 422", async () => {
    backendFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "chunk_id is not a uuid" }), { status: 422 }),
    );
    const response = await GET(passageRequest("?workspace_id=ws-1&chunk_id=c-1"));
    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({
      error: { code: "invalid_request", message: "chunk_id is not a uuid" },
    });
  });
});

describe("normalizePassage", () => {
  it("accepts `content` or the `text` alias", () => {
    expect(normalizePassage({ content: "a" })?.content).toBe("a");
    expect(normalizePassage({ text: "b" })?.content).toBe("b");
  });

  it("nulls malformed provenance instead of echoing garbage", () => {
    const passage = normalizePassage({
      content: "a",
      page: "3",
      span: [1, "2"],
      bbox: [1, 2, 3],
      filename: "",
    });
    expect(passage).toMatchObject({
      content: "a",
      page: null,
      span: null,
      bbox: null,
      filename: null,
    });
  });

  it("returns null when there is no readable text", () => {
    expect(normalizePassage(null)).toBeNull();
    expect(normalizePassage("raw")).toBeNull();
    expect(normalizePassage({ chunk_id: "c-1" })).toBeNull();
    expect(normalizePassage({ content: "" })).toBeNull();
  });
});
