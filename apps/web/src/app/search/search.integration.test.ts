import { describe, expect, it } from "vitest";

const RUN_LIVE = process.env.RUN_LIVE_SEARCH === "1";

describe("search live API (opt-in)", () => {
  it.skipIf(!RUN_LIVE)("placeholder: live integration requires RUN_LIVE_SEARCH=1", () => {
    // Opt-in only: when RUN_LIVE_SEARCH=1 and a backend is reachable, extend
    // this suite to exercise keyword/semantic/hybrid against the fixture corpus.
    expect(true).toBe(true);
  });
});
