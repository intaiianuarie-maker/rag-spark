/**
 * M5-001b REV-2 (police finding 7) — the initial URL search waits for the
 * workspace, then fires exactly once.
 *
 * The finding: the mount-only effect ran while WorkspaceProvider still had
 * active=null, so runSearch returned immediately and the effect never re-ran
 * when the workspace finally resolved — a reload or pasted URL silently did
 * nothing. The REV-1 follow-up pinned the opposite direction: a workspace
 * SWITCH must not re-derive a search from the URL (duplicate requests).
 *
 * Both directions are driven through the real gate module the page effect
 * uses, over the render sequence a provider mount actually produces.
 */
import { describe, expect, it } from "vitest";

import {
  createInitialUrlGate,
  markInitialUrlSearchRan,
  shouldRunInitialUrlSearch,
} from "./initial-url-search";

describe("initial URL search gate", () => {
  it("stays closed while WorkspaceProvider has not resolved the workspace", () => {
    const gate = createInitialUrlGate();
    // Mount render: the provider initializes asynchronously, active is null.
    expect(shouldRunInitialUrlSearch(gate, null)).toBe(false);
    // Still null on a later render — nothing has fired, nothing is lost.
    expect(shouldRunInitialUrlSearch(gate, null)).toBe(false);
    expect(gate.ran).toBe(false);
  });

  // REV-3 residual (POLICE): the page derives the workspace id as
  // `active?.id`, which is UNDEFINED (not null) while active is null. A gate
  // that rejects only `null` opens on undefined, latches, and the workspace
  // resolution that follows can never search. The actual page-derived
  // sequence is pinned here.
  it("stays closed on the page-derived undefined workspace id", () => {
    const gate = createInitialUrlGate();
    const activeId = undefined; // exactly what `active?.id` yields pre-readiness
    expect(shouldRunInitialUrlSearch(gate, activeId)).toBe(false);
    expect(gate.ran).toBe(false); // nothing latched — readiness can still fire
  });

  it("runs exactly once on the undefined -> ws-1 sequence the page produces", () => {
    const gate = createInitialUrlGate();
    // Mount: provider not ready (undefined), then it resolves to ws-1.
    expect(shouldRunInitialUrlSearch(gate, undefined)).toBe(false);
    expect(shouldRunInitialUrlSearch(gate, "ws-1")).toBe(true);
    markInitialUrlSearchRan(gate);
    expect(shouldRunInitialUrlSearch(gate, "ws-1")).toBe(false);
  });

  it("rejects both absent values — undefined never opens, null never opens", () => {
    for (const absent of [null, undefined]) {
      const gate = createInitialUrlGate();
      expect(shouldRunInitialUrlSearch(gate, absent)).toBe(false);
      expect(gate.ran).toBe(false);
      expect(shouldRunInitialUrlSearch(gate, absent)).toBe(false);
      expect(gate.ran).toBe(false);
    }
  });

  it("opens exactly once, the moment the workspace becomes available", () => {
    const gate = createInitialUrlGate();
    expect(shouldRunInitialUrlSearch(gate, null)).toBe(false);

    // The provider resolves (ws-1) — the initial URL search fires now.
    expect(shouldRunInitialUrlSearch(gate, "ws-1")).toBe(true);
    markInitialUrlSearchRan(gate);
    expect(gate.ran).toBe(true);

    // It must not fire again on any later render for the same workspace.
    expect(shouldRunInitialUrlSearch(gate, "ws-1")).toBe(false);
  });

  it("a workspace switch does NOT re-derive the search from the URL", () => {
    const gate = createInitialUrlGate();
    shouldRunInitialUrlSearch(gate, "ws-1");
    markInitialUrlSearchRan(gate);

    // Switch to ws-2: the URL still holds ws-1's query — re-running it would
    // fire a duplicate request under the new workspace.
    expect(shouldRunInitialUrlSearch(gate, "ws-2")).toBe(false);
  });

  it("survives a provider flap (null → ws-1 → null → ws-1) firing once", () => {
    const gate = createInitialUrlGate();
    shouldRunInitialUrlSearch(gate, null);
    expect(shouldRunInitialUrlSearch(gate, "ws-1")).toBe(true);
    markInitialUrlSearchRan(gate);

    // The provider briefly re-resolves to null (e.g. a refresh), then back.
    expect(shouldRunInitialUrlSearch(gate, null)).toBe(false);
    expect(shouldRunInitialUrlSearch(gate, "ws-1")).toBe(false);
    expect(gate.ran).toBe(true);
  });

  it("an empty-URL mount still latches (no search to run, no re-fire later)", () => {
    const gate = createInitialUrlGate();
    // Workspace available, but the URL has no q= — the page latches anyway so
    // a later render (or workspace switch) cannot double-fire.
    expect(shouldRunInitialUrlSearch(gate, "ws-1")).toBe(true);
    markInitialUrlSearchRan(gate);
    expect(shouldRunInitialUrlSearch(gate, "ws-1")).toBe(false);
  });

  it("a fresh mount (reload) starts with the gate open", () => {
    const first = createInitialUrlGate();
    markInitialUrlSearchRan(first);
    // A real reload remounts the page: a new gate, ready to fire again.
    const reloaded = createInitialUrlGate();
    expect(shouldRunInitialUrlSearch(reloaded, "ws-1")).toBe(true);
  });
});
