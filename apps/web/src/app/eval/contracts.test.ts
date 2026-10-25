import { describe, expect, it } from "vitest";

import { formatScore, mayShowSourceText } from "./contracts";

describe("formatScore", () => {
  it("formats finite numbers to 4 decimals", () => {
    expect(formatScore(0.123456)).toBe("0.1235");
    expect(formatScore(0)).toBe("0.0000");
  });
  it("returns dash for null/undefined/non-finite", () => {
    expect(formatScore(null)).toBe("—");
    expect(formatScore(undefined)).toBe("—");
    expect(formatScore(Number.NaN)).toBe("—");
    expect(formatScore(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

describe("mayShowSourceText (privacy opt-in, default off)", () => {
  it("is false unless explicitly opted in", () => {
    expect(mayShowSourceText(false)).toBe(false);
    expect(mayShowSourceText(undefined as unknown as boolean)).toBe(false);
  });
  it("is true only when opted in", () => {
    expect(mayShowSourceText(true)).toBe(true);
  });
});
