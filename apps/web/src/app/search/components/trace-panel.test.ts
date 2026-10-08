/**
 * Unit tests for the RAG debugger trace helpers (TASK-M8-002).
 *
 * Framework-free helpers are testable in the node vitest environment; the bar
 * math and leg-detection are the parts worth pinning down.
 */

import { describe, expect, it } from "vitest";

import { activeLegs, TRACE_KEYS, traceBars } from "./trace-panel";

describe("traceBars", () => {
  it("returns an empty list when there is no trace", () => {
    expect(traceBars(undefined)).toEqual([]);
    expect(traceBars(null)).toEqual([]);
    expect(traceBars({})).toEqual([]);
  });

  it("normalizes the largest present score to 100% width", () => {
    const bars = traceBars({ rrf: 0.5, dense: 0.25 });
    const rrf = bars.find((b) => b.key === "rrf");
    const dense = bars.find((b) => b.key === "dense");
    expect(rrf?.widthPct).toBe(100);
    expect(dense?.widthPct).toBe(50);
  });

  it("omits missing legs rather than drawing a zero bar", () => {
    const bars = traceBars({ rrf: 0.3 });
    expect(bars.map((b) => b.key)).toEqual(["rrf"]);
    // absent legs never appear
    expect(bars.find((b) => b.key === "dense")).toBeUndefined();
  });

  it("orders bars by canonical TRACE_KEYS, not input order", () => {
    const bars = traceBars({ rerank: 0.9, rrf: 0.4, dense: 0.7 });
    const order = bars.map((b) => b.key);
    expect(order).toEqual(
      TRACE_KEYS.filter((k) => order.includes(k)),
    );
  });

  it("guards against a zero max (all-zero scores) without dividing by zero", () => {
    const bars = traceBars({ rrf: 0, dense: 0 });
    expect(bars.every((b) => b.widthPct === 0)).toBe(true);
  });

  it("ignores non-finite values", () => {
    const bars = traceBars({ rrf: 0.5, dense: Number.NaN });
    expect(bars.map((b) => b.key)).toEqual(["rrf"]);
  });
});

describe("activeLegs", () => {
  it("lists only the legs that fired", () => {
    expect(activeLegs({ rrf: 0.2, lexical: 0.1 })).toEqual(["lexical", "rrf"]);
  });

  it("returns an empty list with no trace", () => {
    expect(activeLegs(undefined)).toEqual([]);
  });
});
