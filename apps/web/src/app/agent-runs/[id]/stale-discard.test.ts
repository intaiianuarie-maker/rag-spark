import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const SOURCE = readFileSync(
  new URL("./page.tsx", import.meta.url),
  "utf8",
);

describe("stale-discard guard (T-W01)", () => {
  it("guards post-await setState with a request token", () => {
    expect(SOURCE).toContain("reqRef.current !== tok");
    expect(SOURCE).toContain("const tok = ++reqRef.current;");
  });
});
