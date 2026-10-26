import { describe, expect, it } from "vitest";

describe("sources contracts", () => {
  it("module loads", async () => {
    const mod = await import("./contracts");
    expect(mod).toBeTruthy();
    expect(typeof mod.listSources).toBe("function");
    expect(typeof mod.createSource).toBe("function");
    expect(typeof mod.triggerSourceSync).toBe("function");
    expect(typeof mod.getSourceStatus).toBe("function");
  });
});
