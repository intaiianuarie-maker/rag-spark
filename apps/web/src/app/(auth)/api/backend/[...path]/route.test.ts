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
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const cookieGetMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: cookieGetMock })),
}));

import { forward, DELETE } from "./route";

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
