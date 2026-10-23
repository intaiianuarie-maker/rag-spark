/**
 * T-V1-033 — deleted sources are reported as DELETED (RP-13/QF-13).
 *
 * The panel must be able to say "this source is gone" instead of the vaguer
 * "passage unavailable", and it must only say that when the backend actually
 * proved it: a 404 from the existing, ACL-checked document-detail endpoint.
 * An outage, a forbidden probe, or a citation without a document ref must
 * never be reported as a deletion.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const cookieGetMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: cookieGetMock })),
}));

import { GET, buildDocumentUpstreamUrl, readPassageParams } from "./handlers";

const backendFetch = vi.fn<(input: RequestInfo, init?: RequestInit) => Promise<Response>>();

const CHUNK_URL = "http://api.test:8000/v1/workspaces/ws-1/chunks/chunk-a";
const DOC_URL = "http://api.test:8000/v1/workspaces/ws-1/documents/doc-a";

function request(query: string): Request {
  return new Request(`http://web.local/api/passage${query}`, { method: "GET" });
}

const jsonResponse = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

async function errorBody(response: Response): Promise<{ code: string; message: string }> {
  const body = (await response.json()) as { error: { code: string; message: string } };
  return body.error;
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

describe("buildDocumentUpstreamUrl", () => {
  it("percent-encodes both ids", () => {
    expect(buildDocumentUpstreamUrl("http://api.test:8000", "ws 1", "doc/x")).toBe(
      "http://api.test:8000/v1/workspaces/ws%201/documents/doc%2Fx",
    );
  });
});

describe("readPassageParams with the optional document ref", () => {
  it("accepts it and defaults it to null", () => {
    expect(readPassageParams(new URLSearchParams("workspace_id=ws-1&chunk_id=c-1"))).toEqual({
      ok: true,
      workspaceId: "ws-1",
      chunkId: "c-1",
      documentId: null,
    });
    expect(
      readPassageParams(
        new URLSearchParams("workspace_id=ws-1&chunk_id=c-1&document_id=doc-1"),
      ),
    ).toEqual({ ok: true, workspaceId: "ws-1", chunkId: "c-1", documentId: "doc-1" });
  });

  it("rejects a traversal in the document ref without contacting the backend", async () => {
    const response = await GET(
      request("/api/passage?workspace_id=ws-1&chunk_id=c-1&document_id=..%2Fsecret"),
    );
    expect(response.status).toBe(400);
    expect((await errorBody(response)).code).toBe("invalid_request");
    expect(backendFetch).not.toHaveBeenCalled();
  });
});

describe("deleted-source reporting on the chunk 404 path", () => {
  it("answers source_deleted when the source document is gone", async () => {
    backendFetch
      .mockResolvedValueOnce(jsonResponse(404, { detail: { error: "chunk_not_found" } }))
      .mockResolvedValueOnce(jsonResponse(404, { detail: { error: "document_not_found" } }));

    const response = await GET(
      request("/api/passage?workspace_id=ws-1&chunk_id=chunk-a&document_id=doc-a"),
    );

    expect(response.status).toBe(404);
    const error = await errorBody(response);
    expect(error.code).toBe("source_deleted");
    expect(error.message).toMatch(/no longer in this workspace/i);

    // Exactly two upstream reads: the canonical chunk, then the probe.
    expect(backendFetch).toHaveBeenCalledTimes(2);
    expect(backendFetch.mock.calls[0][0]).toBe(CHUNK_URL);
    expect(backendFetch.mock.calls[1][0]).toBe(DOC_URL);
    // The probe carries the caller's bearer token: still server-side ACL.
    const probeHeaders = backendFetch.mock.calls[1][1]?.headers as Record<string, string>;
    expect(probeHeaders.authorization).toBe("Bearer test-access-token");
  });

  it("does NOT claim deletion while the document is still live", async () => {
    backendFetch
      .mockResolvedValueOnce(jsonResponse(404, { detail: {} }))
      .mockResolvedValueOnce(jsonResponse(200, { id: "doc-a", title: "still here" }));

    const response = await GET(
      request("/api/passage?workspace_id=ws-1&chunk_id=chunk-a&document_id=doc-a"),
    );

    expect(response.status).toBe(404);
    expect((await errorBody(response)).code).toBe("passage_unavailable");
  });

  it("does NOT claim deletion when the probe itself fails (outage ≠ deleted)", async () => {
    backendFetch
      .mockResolvedValueOnce(jsonResponse(404, { detail: {} }))
      .mockResolvedValueOnce(jsonResponse(500, { detail: "boom" }));

    const response = await GET(
      request("/api/passage?workspace_id=ws-1&chunk_id=chunk-a&document_id=doc-a"),
    );
    expect((await errorBody(response)).code).toBe("passage_unavailable");

    backendFetch.mockReset();
    backendFetch
      .mockResolvedValueOnce(jsonResponse(404, { detail: {} }))
      .mockRejectedValueOnce(new Error("connection refused"));
    const second = await GET(
      request("/api/passage?workspace_id=ws-1&chunk_id=chunk-a&document_id=doc-a"),
    );
    expect((await errorBody(second)).code).toBe("passage_unavailable");
  });

  it("does NOT claim deletion when the probe says forbidden", async () => {
    backendFetch
      .mockResolvedValueOnce(jsonResponse(404, { detail: {} }))
      .mockResolvedValueOnce(jsonResponse(403, { detail: { error: "forbidden" } }));

    const response = await GET(
      request("/api/passage?workspace_id=ws-1&chunk_id=chunk-a&document_id=doc-a"),
    );
    expect((await errorBody(response)).code).toBe("passage_unavailable");
    expect(backendFetch).toHaveBeenCalledTimes(2);
  });

  it("keeps the original answer when the citation carries no document ref", async () => {
    backendFetch.mockResolvedValueOnce(jsonResponse(404, { detail: {} }));

    const response = await GET(request("/api/passage?workspace_id=ws-1&chunk_id=chunk-a"));

    expect(response.status).toBe(404);
    expect((await errorBody(response)).code).toBe("passage_unavailable");
    // No document ref → no probe at all.
    expect(backendFetch).toHaveBeenCalledTimes(1);
  });

  it("never probes on a successful canonical read", async () => {
    backendFetch.mockResolvedValueOnce(
      jsonResponse(200, { chunk_id: "chunk-a", content: "text" }),
    );

    const response = await GET(
      request("/api/passage?workspace_id=ws-1&chunk_id=chunk-a&document_id=doc-a"),
    );

    expect(response.status).toBe(200);
    expect(backendFetch).toHaveBeenCalledTimes(1);
  });
});
