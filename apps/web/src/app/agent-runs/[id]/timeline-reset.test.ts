/**
 * T-V3-030 — F-1 fix: the run timeline is reset when the run or workspace
 * changes (T-V3-029 review, finding F-1, MEDIUM).
 *
 * The property: navigating from run A to run B (or switching workspace with a
 * different run in the URL) shows ONLY run B's timeline — run A's observed
 * lifecycle states never render above run B's, because the timeline belongs to
 * exactly one (workspace, run).
 *
 * Two layers of proof, because this repo has no DOM test harness (vitest runs
 * in the node environment, and adding one is a new dev dependency outside this
 * ticket's allowed files):
 *   1. BEHAVIOUR — the reset decision is a pure function (`timelineAfterKeyChange`)
 *      exercised directly against the A→B and same-run-poll traces.
 *   2. WIRING — the page consults exactly that helper and no longer has the
 * `loadedRef` guard that made the bug possible, so the behaviour is actually
 * reached rather than defined and forgotten.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  runStateKey,
  timelineAfterKeyChange,
  type TimelineEntry,
} from "./run-detail";

const A_STATES: TimelineEntry[] = [
  { at: "2026-10-14T10:00:00.000Z", status: "queued", error_code: null },
  { at: "2026-10-14T10:00:03.000Z", status: "running", error_code: null },
  { at: "2026-10-14T10:01:00.000Z", status: "completed", error_code: null },
];

const B_FIRST: TimelineEntry[] = [
  { at: "2026-10-14T11:00:00.000Z", status: "queued", error_code: null },
];

describe("timeline keying (T-V3-030 / F-1)", () => {
  it("the key covers BOTH the workspace and the run id", () => {
    expect(runStateKey("ws-a", "run-1")).toBe("ws-a/run-1");
    expect(runStateKey("ws-b", "run-1")).toBe("ws-b/run-1");
    expect(runStateKey("ws-a", "run-2")).toBe("ws-a/run-2");
    // Different keys for every different (workspace, run) pair.
    expect(runStateKey("ws-a", "run-1")).not.toBe(runStateKey("ws-b", "run-1"));
    expect(runStateKey("ws-a", "run-1")).not.toBe(runStateKey("ws-a", "run-2"));
  });

  it("no key while the workspace or the run is unknown (never a wrong key)", () => {
    expect(runStateKey(null, "run-1")).toBeNull();
    expect(runStateKey("ws-a", null)).toBeNull();
    expect(runStateKey(null, null)).toBeNull();
  });

  it("A→B navigation starts run B from an EMPTY timeline", () => {
    // Page shows run A, its timeline has A's states; run A's key is loaded.
    const after = timelineAfterKeyChange("ws-a/run-a", "ws-a/run-b", A_STATES);
    expect(after).toEqual([]);
    // ...so run B's timeline cannot contain run A's states.
    const bTimeline = [...after, ...B_FIRST];
    expect(bTimeline.map((e) => e.status)).toEqual(["queued"]);
    expect(bTimeline.some((e) => e.status === "completed")).toBe(false);
  });

  it("a workspace switch resets, even with the same run id in the URL", () => {
    // The URL's run id can be identical across workspaces; the scope key is what
    // separates them, so the same-id switch still starts empty.
    expect(timelineAfterKeyChange("ws-a/run-1", "ws-b/run-1", A_STATES)).toEqual([]);
  });

  it("polling the SAME run keeps its history (no reset on refresh)", () => {
    // A poll for the run already on screen must not wipe the timeline — key
    // unchanged, history unchanged (the very object, not a copy).
    const kept = timelineAfterKeyChange("ws-a/run-a", "ws-a/run-a", A_STATES);
    expect(kept).toBe(A_STATES);
    expect(kept).toHaveLength(3);
  });

  it("an unknown workspace/run (null key) never inherits a previous timeline", () => {
    // A null key is not the same key as any real run, so history is dropped
    // rather than attributed to the run that follows.
    expect(timelineAfterKeyChange("ws-a/run-a", null, A_STATES)).toEqual([]);
  });

  it("A→B→A shows A fresh, not the stale A-with-B-appended history", () => {
    let history = A_STATES;
    history = timelineAfterKeyChange("ws-a/run-a", "ws-a/run-b", history); // go to B
    expect(history).toEqual([]);
    history = [...history, ...B_FIRST]; // B's first observed state
    history = timelineAfterKeyChange("ws-a/run-b", "ws-a/run-a", history); // back to A
    expect(history).toEqual([]);
    // A's states are only re-observed by A's own polling.
    const reloaded = [...history, ...A_STATES.slice(0, 1)];
    expect(reloaded.map((e) => e.status)).toEqual(["queued"]);
  });
});

describe("the page is wired to that reset (wiring proof)", () => {
  const page = readFileSync(
    fileURLToPath(new URL("./run-detail.tsx", import.meta.url)),
    "utf8",
  );

  it("consults timelineAfterKeyChange for the reset", () => {
    expect(page).toContain("setHistory((current) => timelineAfterKeyChange(loadedKey, key, current));");
  });

  it("keys the per-run state on workspace AND run id", () => {
    expect(page).toContain("const key = runStateKey(workspaceId, runId);");
    expect(page).toMatch(/if \(key !== loadedKey\) \{/);
  });

  it("resets the rest of the per-run state with the timeline", () => {
    // Without these, run A's status chip, record and 404 alert would keep
    // rendering while run B loads — the same stale-state class as F-1.
    for (const reset of ["setSnapshot(null);", "setRecord(null);", "setNotFound(false);", "setError(null);"]) {
      expect(page).toContain(reset);
    }
  });

  it("loads on key change — the loadedRef guard that caused F-1 is gone", () => {
    // Asserted on the GUARD CODE, not the identifier: the page's own comment
    // names the removed guard for the reader, so a blunt "does not mention
    // loadedRef" check would be defeated by the documentation that explains
    // the fix. What matters is that the once-per-lifetime guard cannot run.
    expect(page).not.toContain("if (loadedRef.current) return;");
    expect(page).not.toContain("loadedRef.current = true;");
    // The load effect is keyed on the run key, so a new workspace/run fires a
    // fresh read. The read is scheduled as a timer callback (setState must not
    // be called directly in an effect body — react-hooks/set-state-in-effect).
    expect(page).toContain("const timer = window.setTimeout(() => void load(), 0);");
    expect(page).toMatch(/return \(\) => window\.clearTimeout\(timer\);\s*\n\s*\}, \[load, key\]\);/);
  });
});
