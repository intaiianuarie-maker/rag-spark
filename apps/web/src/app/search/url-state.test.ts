import { describe, expect, it } from "vitest";

import {
  EMPTY_SEARCH_FILTERS,
  type SearchFilters,
  type SearchMode,
} from "./contracts";
import {
  parseSearchState,
  searchStateToParams,
  searchStateToQueryString,
} from "./url-state";

const ALL_FILTERS: SearchFilters = {
  type: "pdf",
  source: "confluence",
  date: "2026-01-01..2026-03-31",
  collection: "finance",
  language: "en",
};

describe("url-state round-trip", () => {
  it("round-trips all facets (serialize then parse)", () => {
    const params = searchStateToParams({ query: "budget", mode: "semantic", filters: ALL_FILTERS });
    const parsed = parseSearchState(params);
    expect(parsed.query).toBe("budget");
    expect(parsed.mode).toBe("semantic");
    expect(parsed.filters).toEqual(ALL_FILTERS);
  });

  it("round-trips filters alone (parse then serialize is stable)", () => {
    const params = searchStateToParams({ query: "", mode: "hybrid", filters: ALL_FILTERS });
    const qs1 = params.toString();
    const parsed = parseSearchState(params);
    const qs2 = searchStateToParams(parsed).toString();
    expect(qs2).toBe(qs1);
  });

  it("all-empty produces no params", () => {
    const params = searchStateToParams({ query: "", mode: "hybrid", filters: EMPTY_SEARCH_FILTERS });
    expect(params.toString()).toBe("");
  });

  it("parse of empty params yields all-empty state", () => {
    const parsed = parseSearchState(new URLSearchParams());
    expect(parsed.query).toBe("");
    expect(parsed.mode).toBe("hybrid");
    expect(parsed.filters).toEqual(EMPTY_SEARCH_FILTERS);
  });

  it("blank/whitespace facets are treated as absent", () => {
    const filters: SearchFilters = {
      type: "   ",
      source: null,
      date: "  ",
      collection: "",
      language: "en",
    };
    const params = searchStateToParams({ query: "   ", mode: "hybrid", filters });
    expect(params.has("type")).toBe(false);
    expect(params.has("date")).toBe(false);
    expect(params.has("collection")).toBe(false);
    expect(params.has("q")).toBe(false);
    expect(params.get("language")).toBe("en");
    const parsed = parseSearchState(params);
    expect(parsed.filters).toEqual({ ...EMPTY_SEARCH_FILTERS, language: "en" });
    expect(parsed.query).toBe("");
  });

  it("unknown/extra params are ignored without throwing", () => {
    const params = new URLSearchParams("q=hello&foo=bar&tags=x&mode=keyword&type=pdf");
    const parsed = parseSearchState(params);
    expect(parsed.query).toBe("hello");
    expect(parsed.mode).toBe("keyword");
    expect(parsed.filters.type).toBe("pdf");
    expect((parsed as unknown as Record<string, unknown>).foo).toBeUndefined();
  });

  it("invalid mode falls back to hybrid", () => {
    const parsed = parseSearchState(new URLSearchParams("mode=banana"));
    expect(parsed.mode).toBe("hybrid");
  });

  it("accepts string input", () => {
    const parsed = parseSearchState("q=hi&mode=semantic&type=pdf");
    expect(parsed.query).toBe("hi");
    expect(parsed.mode).toBe("semantic");
    expect(parsed.filters.type).toBe("pdf");
  });

  it("accepts object input", () => {
    const parsed = parseSearchState({ q: "hi", mode: "keyword", type: "pdf" });
    expect(parsed.query).toBe("hi");
    expect(parsed.mode).toBe("keyword");
    expect(parsed.filters.type).toBe("pdf");
  });

  it("round-trips every facet combination (all 32 subsets)", () => {
    const keys: (keyof SearchFilters)[] = ["type", "source", "date", "collection", "language"];
    for (let mask = 0; mask < 1 << keys.length; mask++) {
      const filters = { ...EMPTY_SEARCH_FILTERS };
      for (let i = 0; i < keys.length; i++) {
        if (mask & (1 << i)) (filters as Record<string, string | null>)[keys[i]] = `${keys[i]}-val`;
      }
      for (const mode of ["keyword", "semantic", "hybrid"] as SearchMode[]) {
        for (const q of ["", "budget", "  spaced  "]) {
          const state = { query: q.trim() === "" ? "" : q.trim(), mode, filters };
          const params = searchStateToParams(state);
          const parsed = parseSearchState(params);
          expect(parsed).toEqual(state);
          // stability
          expect(searchStateToParams(parsed).toString()).toBe(params.toString());
        }
      }
    }
  });

  it("popstate restore path: parse then serialize stable", () => {
    const qs = "q=hello&mode=semantic&type=pdf&language=en";
    const parsed = parseSearchState(qs);
    const out = searchStateToQueryString(parsed);
    expect(out).toBe("q=hello&mode=semantic&type=pdf&language=en");
  });
});
