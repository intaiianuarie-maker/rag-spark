/**
 * Unit tests for the Search UI contract helpers (TASK-M5-002).
 *
 * Covers the acceptance-critical, pure logic: hybrid-default mode, the
 * empty-filter drop (so the backend never over-narrows), and date-predicate
 * shape validation (exact day or inclusive range).
 *
 * T-V1-009 (RP-06, QF-06): the helper emits a request **object**, never a
 * pre-stringified JSON string, so the Search path has exactly one
 * serialization boundary (owned by auth-client).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_SEARCH_MODE,
  EMPTY_SEARCH_FILTERS,
  SEARCH_MODES,
  buildSearchBody,
  isValidDateFilter,
  searchDocuments,
  type SearchFilters,
} from "./contracts";

const mocks = vi.hoisted(() => ({
  authFetch: vi.fn(),
}));

vi.mock("@/lib/auth-client", () => ({
  auth: { authFetch: mocks.authFetch },
}));

const RESPONSE = { results: [], mode: "hybrid" };

beforeEach(() => {
  mocks.authFetch.mockReset();
  mocks.authFetch.mockResolvedValue(RESPONSE);
});

describe("SEARCH_MODES / default", () => {
  it("exposes exactly the three backend modes", () => {
    expect(SEARCH_MODES).toEqual(["keyword", "semantic", "hybrid"]);
  });

  it("defaults to hybrid", () => {
    expect(DEFAULT_SEARCH_MODE).toBe("hybrid");
  });
});

describe("buildSearchBody", () => {
  it("passes query through and defaults mode to hybrid", () => {
    const body = buildSearchBody("quarterly report");
    expect(body.query).toBe("quarterly report");
    expect(body.mode).toBe("hybrid");
    expect(body.filters).toEqual(EMPTY_SEARCH_FILTERS);
  });

  it("preserves an explicitly selected mode", () => {
    expect(buildSearchBody("q", "keyword").mode).toBe("keyword");
    expect(buildSearchBody("q", "semantic").mode).toBe("semantic");
  });

  it("drops empty-string filters so the backend never over-narrows", () => {
    const body = buildSearchBody("q", "hybrid", {
      type: "",
      source: "   ",
      date: null,
      collection: "",
      language: "",
    });
    expect(body.filters).toEqual({
      type: null,
      source: null,
      date: null,
      collection: null,
      language: null,
    });
  });

  it("trims and keeps non-empty filters", () => {
    const filters: SearchFilters = {
      type: " pdf ",
      source: "confluence",
      date: null,
      collection: null,
      language: "en",
    };
    const body = buildSearchBody("q", "hybrid", filters);
    expect(body.filters.type).toBe("pdf");
    expect(body.filters.source).toBe("confluence");
    expect(body.filters.language).toBe("en");
  });
});

describe("isValidDateFilter", () => {
  it("treats null/empty as valid (unset filter)", () => {
    expect(isValidDateFilter(null)).toBe(true);
    expect(isValidDateFilter(undefined)).toBe(true);
    expect(isValidDateFilter("")).toBe(true);
    expect(isValidDateFilter("   ")).toBe(true);
  });

  it("accepts an exact ISO day", () => {
    expect(isValidDateFilter("2026-01-15")).toBe(true);
  });

  it("accepts an inclusive range", () => {
    expect(isValidDateFilter("2026-01-01..2026-03-31")).toBe(true);
  });

  it("rejects malformed shapes", () => {
    expect(isValidDateFilter("2026-1-5")).toBe(false);
    expect(isValidDateFilter("01/15/2026")).toBe(false);
    expect(isValidDateFilter("2026-01-01..")).toBe(false);
    expect(isValidDateFilter("2026-01-01-2026-03-31")).toBe(false);
    expect(isValidDateFilter("not-a-date")).toBe(false);
  });
});

describe("searchDocuments → authFetch (T-V1-009)", () => {
  it("passes an object body, not JSON.stringify output", async () => {
    await searchDocuments("ws-1", buildSearchBody("quarterly report"));

    expect(mocks.authFetch).toHaveBeenCalledTimes(1);
    const [path, init] = mocks.authFetch.mock.calls[0] as [
      string,
      { method: string; body: unknown },
    ];

    expect(path).toBe("/v1/workspaces/ws-1/search");
    expect(init.method).toBe("POST");
    expect(typeof init.body).toBe("object");
    expect(init.body).not.toBeInstanceOf(String);
    expect(init.body).toEqual({
      query: "quarterly report",
      mode: "hybrid",
      filters: EMPTY_SEARCH_FILTERS,
    });
    // The exact shape backend SearchRequest parses after the single auth-client
    // stringify: one object, one serialization boundary.
    expect(JSON.parse(JSON.stringify(init.body))).toEqual({
      query: "quarterly report",
      mode: "hybrid",
      filters: {
        type: null,
        source: null,
        date: null,
        collection: null,
        language: null,
      },
    });
  });

  it("emits a plain object from buildSearchBody (never a string)", () => {
    const body = buildSearchBody("q", "keyword");
    expect(typeof body).toBe("object");
    expect(typeof body).not.toBe("string");
    expect(body).not.toBeInstanceOf(String);
  });

  it("forwards mode + non-empty filters as object fields", async () => {
    await searchDocuments(
      "ws-1",
      buildSearchBody("q", "semantic", {
        type: "pdf",
        source: null,
        date: "2026-01-01..2026-03-31",
        collection: null,
        language: "en",
      }),
    );

    const [, init] = mocks.authFetch.mock.calls[0] as [
      string,
      { body: unknown },
    ];
    expect(init.body).toEqual({
      query: "q",
      mode: "semantic",
      filters: {
        type: "pdf",
        source: null,
        date: "2026-01-01..2026-03-31",
        collection: null,
        language: "en",
      },
    });
  });

  it("returns the typed SearchResponse payload unchanged", async () => {
    mocks.authFetch.mockResolvedValueOnce({ results: [], mode: "keyword" });
    const res = await searchDocuments("ws-1", buildSearchBody("q", "keyword"));
    expect(res).toEqual({ results: [], mode: "keyword" });
  });

  it("still surfaces authFetch failures (error handling preserved)", async () => {
    mocks.authFetch.mockRejectedValueOnce(new Error("boom"));
    await expect(searchDocuments("ws-1", buildSearchBody("q"))).rejects.toThrow(
      "boom",
    );
  });
});
