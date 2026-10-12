"use client";

/**
 * RAG debugger trace view (TASK-M8-002) — per-result retrieval trace.
 *
 * Renders the frozen debugger trace contract: for every returned chunk the six
 * per-leg scores the orchestrator always carries (`dense`, `sparse`, `lexical`,
 * `graph`, `rrf`, `rerank`). `rrf` is always present; the four leg scores appear
 * exactly when that leg surfaced the chunk; `rerank` only when reranking ran. So the
 * panel shows which retrieval legs contributed and how the final fusion (`rrf`)
 * ranked the chunk — the "why is this result here" answer.
 *
 * Deliberately framework-free + side-effect-free so the bar math is unit-tested
 * in the node vitest environment (see trace-panel.test.ts).
 */

/** Canonical trace keys, in the order the debugger displays them (mirrors orchestrator TRACE_KEYS). */
export const TRACE_KEYS = ["dense", "sparse", "lexical", "graph", "rrf", "rerank"] as const;

export type TraceKey = (typeof TRACE_KEYS)[number];

/** Human labels for each trace leg. */
export const TRACE_LABELS: Record<TraceKey, string> = {
  dense: "Dense",
  sparse: "Sparse",
  lexical: "Lexical",
  graph: "Graph",
  rrf: "Fusion (RRF)",
  rerank: "Rerank",
};

/**
 * Normalize each leg score to a 0–1 bar width relative to the largest present
 * score. Missing legs are omitted (never a zero bar) so absence is visually
 * distinct from a low score. Returns entries in TRACE_KEYS order.
 */
export function traceBars(scores: Record<string, number> | undefined | null): {
  key: TraceKey;
  value: number;
  widthPct: number;
}[] {
  if (!scores) return [];
  const present = TRACE_KEYS.filter(
    (k) => typeof scores[k] === "number" && Number.isFinite(scores[k]),
  );
  if (present.length === 0) return [];
  const max = Math.max(...present.map((k) => scores[k]));
  return present.map((key) => ({
    key,
    value: scores[key],
    widthPct: max > 0 ? Math.round((scores[key] / max) * 100) : 0,
  }));
}

/** Legs that actually fired for a chunk (present in the trace). */
export function activeLegs(scores: Record<string, number> | undefined | null): TraceKey[] {
  if (!scores) return [];
  return TRACE_KEYS.filter(
    (k) => typeof scores[k] === "number" && Number.isFinite(scores[k]),
  );
}
