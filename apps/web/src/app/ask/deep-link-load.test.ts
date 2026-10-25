/**
 * TASK-M4-002b — Ask page deep-link LOAD wiring (behavioral pin).
 *
 * The page must open the right passage panel when a pasted URL carries a
 * `#source=` hash, hydrate after the transcript loads, tolerate garbage /
 * cross-workspace hashes, and re-run on in-page hash navigation.
 *
 * vitest runs in node (no DOM harness), so the wiring is pinned against the
 * screen's own source — the BEHAVIOR behind parseSourceRef is covered for
 * real in deep-link.test.ts.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const page = readFileSync(fileURLToPath(new URL("./page.tsx", import.meta.url)), "utf8");

describe("deep-link load wiring", () => {
  it("imports parseSourceRef from the source panel", () => {
    expect(page).toContain("parseSourceRef");
  });

  it("reads window.location.hash and parses it against the active workspace", () => {
    expect(page).toContain("window.location.hash");
    expect(page).toMatch(/parseSourceRef\(hash,\s*workspaceId\)/);
  });

  it("resolves the citation from the hydrated transcript (never a global)", () => {
    expect(page).toMatch(/messages\[ref\.messageIndex\]/);
    expect(page).toMatch(/msg\?\.citations\.find\(\(c\) => c\.chunk_id === ref\.chunkId\)/);
  });

  it("opens the panel and scrolls the owning message into view", () => {
    expect(page).toContain("setSelectedSource(cit)");
    expect(page).toContain("scrollIntoView");
    expect(page).toContain("`message-${ref.messageIndex}`");
  });

  it("retries until the transcript hydrates (does not drop the link)", () => {
    expect(page).toContain("transcript not hydrated yet");
  });

  it("re-runs on hash navigation (back/forward/paste)", () => {
    expect(page).toContain('addEventListener("hashchange"');
  });

  it("leaves the panel closed on garbage / foreign-workspace hashes", () => {
    expect(page).toContain("lastHashRef.current = null");
  });
});
