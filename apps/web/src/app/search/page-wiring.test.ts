/**
 * T-IMP-003 — the Search screen invalidates in-flight work and guards the
 * loading state when the active workspace changes.
 *
 * Contract pinned here (`apps/web/src/app/search/page.tsx`):
 *
 * 1. **Request invalidation** — switching the active workspace bumps the run
 *    id, so a response/error from the previous workspace can never land
 *    under the new one.
 * 2. **Loading guard** — `loading` / `error` / `response` are only written by
 *    the run that is still current; a superseded run writes nothing.
 * 3. **No setState cascade in an effect** — the result surface is reset when
 *    the workspace changes, but NOT by a synchronous setState cascade inside
 *    an effect body (that is what `react-hooks/set-state-in-effect` rejects:
 *    it triggers an extra cascading render pass on every workspace switch).
 *
 * The wiring is asserted against the screen's own source, the same way
 * `src/app/ask/page-wiring.test.ts` pins the Ask screen: this repo has no DOM
 * test harness (vitest runs in the node environment; adding one would mean a
 * new dev dependency, which is outside this ticket's allowed files).
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const page = readFileSync(fileURLToPath(new URL("./page.tsx", import.meta.url)), "utf8");

/** The body of the workspace-change effect (the first `useEffect` block). */
function workspaceEffect(): string {
  const start = page.indexOf("useEffect(");
  expect(start).toBeGreaterThan(-1);
  const end = page.indexOf("}, [", start);
  expect(end).toBeGreaterThan(start);
  return page.slice(start, end);
}

describe("T-IMP-003: workspace switch invalidates the in-flight request", () => {
  it("bumps the run id when the active workspace changes", () => {
    // The effect body invalidates the in-flight request (run id +1) and is
    // keyed on the active workspace id.
    expect(workspaceEffect()).toMatch(/runIdRef\.current\s*\+=\s*1;/);
    expect(page).toMatch(/\}, \[active(?:Id|\?\.id)\]\);/);
  });

  it("never writes state synchronously inside the workspace-change effect", () => {
    // A setState cascade in an effect body is exactly what
    // `react-hooks/set-state-in-effect` rejects: it cascades an extra render
    // pass on every workspace switch. The reset is applied during render.
    const body = workspaceEffect();
    expect(body).not.toMatch(/setResponse\(/);
    expect(body).not.toMatch(/setSelected\(/);
    expect(body).not.toMatch(/setError\(/);
    expect(body).not.toMatch(/setLoading\(/);
  });

  it("resets the result surface when the workspace changes", () => {
    // The reset is applied when the active workspace id changes, outside the
    // effect body (render-phase adjustment).
    expect(page).toMatch(
      /if \([^)]*activeId[^)]*\) \{[\s\S]{0,600}setResponse\(null\)[\s\S]{0,600}setSelected\(null\)[\s\S]{0,600}setError\(null\)[\s\S]{0,600}setLoading\(false\)/,
    );
  });
});

describe("T-IMP-003: a superseded run never writes state", () => {
  it("guards the loading state with the current run id", () => {
    expect(page).toMatch(/if \(runIdRef\.current === thisRun\) \{\s*setLoading\(false\);/);
  });

  it("drops a stale response or error instead of applying it", () => {
    expect(page).toMatch(/if \(runIdRef\.current !== thisRun\) return;/);
    // The stale guard also covers the empty-query error path.
    expect(page).toMatch(
      /if \(runIdRef\.current === thisRun\) \{\s*setError\("Enter a query to search\."\);/,
    );
  });
});
