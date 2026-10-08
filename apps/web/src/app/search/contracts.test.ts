/**
 * Unit tests for the Search UI contract helpers (TASK-M5-002).
 *
 * Covers the acceptance-critical, pure logic: hybrid-default mode, the
 * empty-filter drop (so the backend never over-narrows), and date-predicate
 * shape validation (exact day or inclusive range).
 */
import { describe, expect, it } from "vitest";
import {
  DEFAULT_SEARCH_MODE,
  EMPTY_SEARCH_FILTERS,
  SEARCH_MODES,
  buildSearchBody,
  isValidDateFilter,
  type SearchFilters,
} from "./contracts";

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
