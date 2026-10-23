/**
 * T-V3-032 — F-3 fix: a cleared limit field falls back to DEFAULT_LIMITS for
 * THAT field, max_tool_calls included.
 *
 * Finding F-3 (T-V3-029 review, LOW): max_steps and max_tokens fell back to
 * DEFAULT_LIMITS on a cleared field, but max_tool_calls did not — so clearing
 * the tool-call input started a run with 0 (no tool calls at all) with no
 * warning. A silent tool-less run is the class of thing this page refuses
 * elsewhere, so the three fields must fall back identically.
 *
 * Properties pinned:
 *  1. every field falls back to its OWN default on blank/non-numeric input;
 *  2. a parsed value is kept verbatim — including a deliberate 0 for
 *     max_tool_calls, which the API legitimately accepts (ge=0). This is why
 *     the old `|| DEFAULT` trick was wrong in BOTH directions.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { startRunLimit } from "./agent-runs";

const DEFAULTS = { max_steps: 5, max_tokens: 100000, max_tool_calls: 50 };

describe("limit fields fall back to their own default (F-3)", () => {
  it("a cleared field (empty string) falls back for every field", () => {
    expect(startRunLimit("", DEFAULTS.max_steps)).toBe(DEFAULTS.max_steps);
    expect(startRunLimit("", DEFAULTS.max_tokens)).toBe(DEFAULTS.max_tokens);
    // The regression this ticket fixes: previously 0 (no fallback at all).
    expect(startRunLimit("", DEFAULTS.max_tool_calls)).toBe(DEFAULTS.max_tool_calls);
  });

  it("a non-numeric field falls back too (parseInt yields NaN)", () => {
    for (const raw of ["abc", "-", "abc123", "  "]) {
      expect(startRunLimit(raw, DEFAULTS.max_steps)).toBe(DEFAULTS.max_steps);
      expect(startRunLimit(raw, DEFAULTS.max_tool_calls)).toBe(DEFAULTS.max_tool_calls);
    }
  });

  it("a parsed value is kept verbatim — including a deliberate 0", () => {
    // max_tool_calls=0 is a legitimate, meaningful value (the API accepts
    // ge=0), so it must NOT be replaced by the default.
    expect(startRunLimit("0", DEFAULTS.max_tool_calls)).toBe(0);
    expect(startRunLimit("7", DEFAULTS.max_tool_calls)).toBe(7);
    expect(startRunLimit("12", DEFAULTS.max_steps)).toBe(12);
    expect(startRunLimit("4096", DEFAULTS.max_tokens)).toBe(4096);
  });

  it("a numeric string with surrounding whitespace still parses", () => {
    expect(startRunLimit(" 7 ", DEFAULTS.max_tool_calls)).toBe(7);
  });

  it("the three defaults are the ones the page declares", () => {
    // Pinned against the source so a changed DEFAULT_LIMITS cannot drift from
    // the values the form seeds its inputs with.
    const page = readFileSync(
      fileURLToPath(new URL("./agent-runs.tsx", import.meta.url)),
      "utf8",
    );
    const match = page.match(
      /DEFAULT_LIMITS = \{[^}]*max_steps:\s*(\d+),\s*max_tokens:\s*(\d+),\s*max_tool_calls:\s*(\d+)/,
    );
    expect(match).not.toBeNull();
    expect(Number(match?.[1])).toBe(DEFAULTS.max_steps);
    expect(Number(match?.[2])).toBe(DEFAULTS.max_tokens);
    expect(Number(match?.[3])).toBe(DEFAULTS.max_tool_calls);
  });

  it("the submit path uses the helper for all three fields", () => {
    const page = readFileSync(
      fileURLToPath(new URL("./agent-runs.tsx", import.meta.url)),
      "utf8",
    );
    // The old bug, asserted impossible: a bare parseInt with no fallback.
    expect(page).not.toContain("max_tool_calls: Number.parseInt(maxToolCalls, 10),");
    expect(page).not.toContain("|| DEFAULT_LIMITS");
    for (const field of ["maxSteps", "maxTokens", "maxToolCalls"]) {
      expect(page).toContain(`startRunLimit(${field}, DEFAULT_LIMITS.`);
    }
  });
});
