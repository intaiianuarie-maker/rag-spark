/**
 * M5-001b REV-1/REV-2 — reload/pasted URL with q+filters must run once the
 * workspace IS available.
 *
 * The mount-only effect used to call runSearch while WorkspaceProvider still
 * had active=null, so runSearch returned immediately and the effect never
 * re-ran when the workspace finally resolved — a reload or pasted URL with
 * q+filters silently did nothing.
 *
 * The fix depends on workspace readiness and is idempotent: the initial
 * URL-derived search fires exactly once per mount, the moment the active
 * workspace becomes available. A subsequent active change does NOT re-derive a
 * search from the URL (the URL already holds the user's last query and
 * re-running it on every switch would fire duplicate requests).
 *
 * REV-3 moved the readiness decision into the extracted gate module
 * (`./initial-url-search`) so the delayed-initialization interleaving is
 * pinned by a REAL behavioral test (`initial-url-search.test.ts`); these
 * source pins follow the wiring to that module. Pinned the same way
 * `page-wiring.test.ts` pins the search screen: against the screen's own
 * source, because this repo has no DOM test harness (vitest runs in the node
 * environment; adding one would mean a new dev dependency, which is outside
 * this ticket's allowed files).
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const page = readFileSync(
  fileURLToPath(new URL(process.env.SEARCH_PAGE_PATH ?? "./page.tsx", import.meta.url)),
  "utf8",
);

describe("M5-001b REV-1/REV-2: the initial URL search waits for the workspace", () => {
  it("depends on workspace readiness, not on mount alone", () => {
    // The effect's decision goes through the gate with the ACTIVE workspace
    // id: while the provider has not resolved (activeId === null) the gate is
    // closed and the effect returns early, deferring the search. The gate's
    // behavior itself is pinned in initial-url-search.test.ts.
    expect(page).toMatch(/shouldRunInitialUrlSearch\(initialUrlGateRef\.current, activeId\)/);
    expect(page).toMatch(/if \(!shouldRunInitialUrlSearch\(/);
  });

  it("is idempotent — the URL search fires once per mount, not on every active change", () => {
    // The gate is latched before the search runs, so a re-render or an active
    // change while the search is in flight cannot re-fire it.
    expect(page).toMatch(/markInitialUrlSearchRan\(initialUrlGateRef\.current\);/);
    // The dependency list is the workspace id (and runSearch), NOT the query:
    // a workspace switch must not re-derive a search from the URL.
    expect(page).toMatch(/\}, \[activeId, runSearch\]\);/);
  });

  it("re-derives query/mode/filters from the URL on mount", () => {
    expect(page).toMatch(/parseSearchState\(window\.location\.search\)/);
    expect(page).toMatch(/void runSearch\(initial\.query, initial\.mode, initial\.filters\)/);
  });
});