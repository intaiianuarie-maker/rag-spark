/**
 * T-V1-032 — passage fetch + span highlight helpers (RP-13 / QF-13).
 *
 * Reviewer checklist pinned here: (2) the passage shown belongs to the
 * citation that was clicked (canonical fetch first, that same citation's own
 * answer-embedded text only as a labeled fallback), and (3) loading / error /
 * empty are all reachable, explicit states.
 */
import { describe, expect, it } from "vitest";

import {
  buildPassageUrl,
  requestPassage,
  resolvePassage,
  segmentPassage,
  type PassageResult,
} from "./components/source-panel";
import type { MessageCitation } from "./components/chat-window";

function citation(overrides: Partial<MessageCitation> = {}): MessageCitation {
  return {
    chunk_id: "chunk-a",
    document_id: "doc-a",
    filename: "alpha.pdf",
    page: 3,
    section: null,
    span: null,
    workspace_id: "ws-1",
    content: null,
    ...overrides,
  };
}

const ok = (text: string): PassageResult => ({ ok: true, text, passage: { content: text } });
const fail = (code: string, message = "boom"): PassageResult => ({ ok: false, code, message });

describe("buildPassageUrl", () => {
  it("encodes both refs so ids can never alter the path", () => {
    expect(buildPassageUrl("ws-1", "chunk-a")).toBe(
      "/api/passage?workspace_id=ws-1&chunk_id=chunk-a",
    );
    expect(buildPassageUrl("ws/../1", "a b")).toBe(
      `/api/passage?${new URLSearchParams({ workspace_id: "ws/../1", chunk_id: "a b" })}`,
    );
  });
});

describe("requestPassage", () => {
  const jsonResponse = (status: number, body: unknown): Response =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });

  it("reads passage text from a 200 envelope", async () => {
    const result = await requestPassage("ws-1", "chunk-a", async () =>
      jsonResponse(200, { ok: true, passage: { chunk_id: "chunk-a", content: "canonical text" } }),
    );
    expect(result).toEqual({
      ok: true,
      text: "canonical text",
      passage: { chunk_id: "chunk-a", content: "canonical text" },
    });
  });

  it("passes the route's explicit failure envelope through (404 pending T-V1-022)", async () => {
    const result = await requestPassage("ws-1", "chunk-a", async () =>
      jsonResponse(404, {
        ok: false,
        error: { code: "passage_unavailable", message: "canonical source not deployed yet" },
      }),
    );
    expect(result).toEqual({
      ok: false,
      code: "passage_unavailable",
      message: "canonical source not deployed yet",
    });
  });

  it("degrades to a status-derived code when the body is not JSON", async () => {
    const result = await requestPassage("ws-1", "chunk-a", async () =>
      new Response("<html>proxy error</html>", { status: 502 }),
    );
    expect(result).toEqual({ ok: false, code: "http_502", message: expect.stringContaining("502") });
  });

  it("reports a 200 without readable text as passage_malformed", async () => {
    const result = await requestPassage("ws-1", "chunk-a", async () =>
      jsonResponse(200, { ok: true, passage: { chunk_id: "chunk-a" } }),
    );
    expect(result).toMatchObject({ ok: false, code: "passage_malformed" });
  });

  it("folds network failures into the same failure shape", async () => {
    const result = await requestPassage("ws-1", "chunk-a", async () => {
      throw new Error("ECONNREFUSED");
    });
    expect(result).toEqual({ ok: false, code: "network_error", message: "ECONNREFUSED" });
  });

  it("requests the exact refs for the clicked citation", async () => {
    const seen: string[] = [];
    await requestPassage("ws-9", "chunk-z", async (input) => {
      seen.push(input);
      return jsonResponse(200, { ok: true, passage: { content: "x" } });
    });
    expect(seen).toEqual([buildPassageUrl("ws-9", "chunk-z")]);
  });
});

describe("resolvePassage — canonical first, own text only as fallback", () => {
  it("uses the live canonical passage when the fetch succeeded", () => {
    expect(resolvePassage(ok("canonical"), citation({ content: "stale" }))).toEqual({
      state: "ready",
      text: "canonical",
      origin: "canonical",
      notice: null,
    });
  });

  it("keeps 404 explicit while falling back to THIS citation's own text", () => {
    const view = resolvePassage(fail("passage_unavailable", "not deployed yet"), citation({ content: "…revenue grew 12%…" }));
    expect(view).toEqual({
      state: "ready",
      text: "…revenue grew 12%…",
      origin: "answer_embedded",
      notice: "passage_unavailable: not deployed yet",
    });
  });

  it("renders an explicit error state when there is nothing to fall back to", () => {
    expect(resolvePassage(fail("forbidden", "workspace scope denied"), citation({ content: null }))).toEqual({
      state: "error",
      code: "forbidden",
      message: "workspace scope denied",
    });
  });

  it("renders an explicit empty state for a successful-but-empty canonical passage", () => {
    const view = resolvePassage(ok("   "), citation({ content: null }));
    expect(view.state).toBe("empty");
    expect(view).toMatchObject({ notice: expect.stringContaining("empty") });
  });

  it("falls back with a notice when the canonical passage came back blank", () => {
    const view = resolvePassage(ok(""), citation({ content: "own text" }));
    expect(view).toMatchObject({ state: "ready", origin: "answer_embedded" });
    expect(view.state === "ready" && view.notice).toContain("empty");
  });
});

describe("segmentPassage — span highlight", () => {
  it("splits the cited span out of the passage", () => {
    expect(segmentPassage("abcdef", [2, 4])).toEqual([
      { text: "ab", hit: false },
      { text: "cd", hit: true },
      { text: "ef", hit: false },
    ]);
  });

  it("handles a span at the very start and very end", () => {
    expect(segmentPassage("abc", [0, 1])).toEqual([
      { text: "a", hit: true },
      { text: "bc", hit: false },
    ]);
    expect(segmentPassage("abc", [2, 3])).toEqual([
      { text: "ab", hit: false },
      { text: "c", hit: true },
    ]);
  });

  it("returns the whole text unhighlighted when there is no span", () => {
    expect(segmentPassage("abc", null)).toEqual([{ text: "abc", hit: false }]);
    expect(segmentPassage("abc", undefined)).toEqual([{ text: "abc", hit: false }]);
  });

  it("clamps out-of-range offsets instead of throwing", () => {
    expect(segmentPassage("abc", [-5, 99])).toEqual([{ text: "abc", hit: true }]);
    expect(segmentPassage("abc", [1, 99])).toEqual([
      { text: "a", hit: false },
      { text: "bc", hit: true },
    ]);
  });

  it("treats inverted or degenerate spans as no highlight", () => {
    expect(segmentPassage("abc", [2, 1])).toEqual([{ text: "abc", hit: false }]);
    expect(segmentPassage("abc", [1, 1])).toEqual([{ text: "abc", hit: false }]);
  });

  it("returns no segments for an empty passage", () => {
    expect(segmentPassage("", [0, 3])).toEqual([]);
  });
});
