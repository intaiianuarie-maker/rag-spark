/**
 * Unit tests for the Ask UI contract helper (T-V1-008 — RP-06, QF-06).
 *
 * The ticket's success criterion: the helper emits a request **object**
 * (`typeof body === "object"`), never a pre-stringified JSON string, so the
 * Ask path has exactly one serialization boundary (owned by auth-client).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { askQuestion, buildAskBody } from "./contracts";

const mocks = vi.hoisted(() => ({
  authFetch: vi.fn(),
}));

vi.mock("@/lib/auth-client", () => ({
  auth: { authFetch: mocks.authFetch },
}));

const ANSWER = {
  answer: "…",
  status: "answered" as const,
  claims: [],
  citations: [],
};

beforeEach(() => {
  mocks.authFetch.mockReset();
  mocks.authFetch.mockResolvedValue(ANSWER);
});

describe("buildAskBody", () => {
  it("returns a plain object (never a string)", () => {
    const body = buildAskBody("what is RP-06?");
    expect(typeof body).toBe("object");
    expect(typeof body).not.toBe("string");
    expect(body).toEqual({ query: "what is RP-06?" });
  });

  it("adds model only when provided", () => {
    expect(buildAskBody("q", "gpt-x")).toEqual({ query: "q", model: "gpt-x" });
    expect(buildAskBody("q", undefined)).toEqual({ query: "q" });
    expect(buildAskBody("q", "")).toEqual({ query: "q" });
  });
});

describe("askQuestion → authFetch", () => {
  it("passes an object body, not JSON.stringify output", async () => {
    await askQuestion("ws-1", "what is RP-06?", "gpt-x");

    expect(mocks.authFetch).toHaveBeenCalledTimes(1);
    const [path, init] = mocks.authFetch.mock.calls[0] as [
      string,
      { method: string; body: unknown },
    ];

    expect(path).toBe("/v1/workspaces/ws-1/ask");
    expect(init.method).toBe("POST");
    expect(typeof init.body).toBe("object");
    expect(init.body).not.toBeInstanceOf(String);
    expect(init.body).toEqual({ query: "what is RP-06?", model: "gpt-x" });
    // The exact shape backend AskRequest parses: {query: str, model?: str}.
    expect(JSON.parse(JSON.stringify(init.body))).toEqual({
      query: "what is RP-06?",
      model: "gpt-x",
    });
  });

  it("omits model when none is selected", async () => {
    await askQuestion("ws-1", "plain query");
    const [, init] = mocks.authFetch.mock.calls[0] as [
      string,
      { body: unknown },
    ];
    expect(init.body).toEqual({ query: "plain query" });
  });

  it("still surfaces authFetch failures (error handling preserved)", async () => {
    mocks.authFetch.mockRejectedValueOnce(new Error("boom"));
    await expect(askQuestion("ws-1", "q")).rejects.toThrow("boom");
  });
});
