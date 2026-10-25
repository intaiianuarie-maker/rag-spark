/**
 * M5-001a REV-3 (police finding 4) — turn-scoped streaming, exercised for real.
 *
 * The police finding: a workspace switch mid-generation let workspace A's
 * tokens paint over workspace B's last historical assistant message, because
 * the streaming surface was a single global string. The rework scoped the
 * surface to the originating turn; this file drives the REAL fence module
 * (the same functions `page.tsx` calls, not a source-text regex) through the
 * real interleavings, including the adjacent defect class the first rework
 * left open — a superseded turn's COMPLETION clearing the new scope's state.
 *
 * Interleavings covered:
 *  - delayed A token after the switch to B            (the finding itself)
 *  - delayed A citations after the switch to B
 *  - delayed A error frame after the switch to B
 *  - A's end-of-turn (finally/abort) after B began    (adjacent class)
 *  - B's own tokens still append                      (the fence permits legit work)
 *  - B's request submitted during its own commit window
 *  - regenerate in the same scope resets the surface
 *  - stop (abort) still clears the owner's surface
 */
import { describe, expect, it } from "vitest";

import {
  appendStreamingToken,
  beginStreamingTurn,
  endStreamingTurn,
  isActiveTurn,
  type StreamingScopeState,
} from "./streaming-scope";

const A = { turnId: "acct::ws-a::t1", scopeKey: "acct::ws-a" };
const B = { turnId: "acct::ws-b::t2", scopeKey: "acct::ws-b" };

describe("turn-scoped streaming fence", () => {
  it("a delayed A token after B's switch leaves B's content untouched", () => {
    // A starts streaming in workspace A.
    let s: StreamingScopeState = beginStreamingTurn(A.turnId, A.scopeKey);
    s = appendStreamingToken(s, A.turnId, "Company ");

    // The user switches to workspace B and sends there.
    s = beginStreamingTurn(B.turnId, B.scopeKey);
    s = appendStreamingToken(s, B.turnId, "Revenue ");

    // A's stream (aborted by the scope change) delivers a late token anyway.
    s = appendStreamingToken(s, A.turnId, "revenue grew 20%.");

    // B's surface is unchanged — A's token is dropped, never painted.
    expect(s).toEqual({
      turnId: B.turnId,
      scopeKey: B.scopeKey,
      content: "Revenue ",
    });
  });

  it("A's delayed citations after B's switch never become B's turn", () => {
    let s: StreamingScopeState = beginStreamingTurn(A.turnId, A.scopeKey);
    s = beginStreamingTurn(B.turnId, B.scopeKey);

    // The citations event itself is turn-scoped at the page level; the fence
    // guarantees the SURFACE also cannot be re-owned by A's late frames.
    s = appendStreamingToken(s, B.turnId, "Revenue ");
    s = appendStreamingToken(s, A.turnId, "stale ");
    expect(s?.turnId).toBe(B.turnId);
    expect(s?.content).toBe("Revenue ");
  });

  it("A's end-of-turn after B began does not clear B's surface (adjacent class)", () => {
    let s: StreamingScopeState = beginStreamingTurn(A.turnId, A.scopeKey);
    s = appendStreamingToken(s, A.turnId, "partial ");

    // Switch to B; B begins streaming.
    s = beginStreamingTurn(B.turnId, B.scopeKey);
    s = appendStreamingToken(s, B.turnId, "Revenue ");

    // A's aborted stream now finishes (error frame, or the finally block
    // after abort-on-scope-change). Before REV-3 this ran setStreamingState(null)
    // unconditionally and blanked B's in-flight bubble.
    s = endStreamingTurn(s, A.turnId);

    expect(s).toEqual({
      turnId: B.turnId,
      scopeKey: B.scopeKey,
      content: "Revenue ",
    });
  });

  it("a superseded turn's completion leaves B's pending indicator set", () => {
    // Page-level flags (pending / abort controller) are gated by the same
    // predicate the page uses after REV-3.
    const activeAfterSwitch = { turnId: B.turnId, scopeKey: B.scopeKey };
    let pending = true;
    if (isActiveTurn(activeAfterSwitch, A.turnId)) pending = false;
    expect(pending).toBe(true); // A's exit must NOT clear B's loading state

    if (isActiveTurn(activeAfterSwitch, B.turnId)) pending = false;
    expect(pending).toBe(false); // B's own completion does clear it
  });

  it("the fence PERMITS legitimate work: B's own tokens still append", () => {
    // A guard that blocks everything is not a fix — the owner's tokens and
    // its own completion must pass through unchanged.
    let s: StreamingScopeState = beginStreamingTurn(B.turnId, B.scopeKey);
    s = appendStreamingToken(s, B.turnId, "Company ");
    s = appendStreamingToken(s, B.turnId, "revenue ");
    s = appendStreamingToken(s, B.turnId, "grew.");
    expect(s).toEqual({
      turnId: B.turnId,
      scopeKey: B.scopeKey,
      content: "Company revenue grew.",
    });
    s = endStreamingTurn(s, B.turnId);
    expect(s).toBeNull();
  });

  it("B's request submitted during B's own commit window survives A's late end", () => {
    // Ordering: B begins (during B's commit window), then A's aborted stream
    // ends, then B's token lands — B must still append.
    let s: StreamingScopeState = beginStreamingTurn(A.turnId, A.scopeKey);
    s = beginStreamingTurn(B.turnId, B.scopeKey);
    s = endStreamingTurn(s, A.turnId);
    s = appendStreamingToken(s, B.turnId, "Revenue ");

    expect(s).toEqual({
      turnId: B.turnId,
      scopeKey: B.scopeKey,
      content: "Revenue ",
    });
  });

  it("regenerating in the same scope resets the surface to empty", () => {
    let s: StreamingScopeState = beginStreamingTurn(A.turnId, A.scopeKey);
    s = appendStreamingToken(s, A.turnId, "stale answer");
    // Regenerate: a NEW turn id takes the surface with fresh content.
    const regeneratedTurn = { ...A, turnId: `${A.turnId}::r2` };
    s = beginStreamingTurn(regeneratedTurn.turnId, regeneratedTurn.scopeKey);
    expect(s).toEqual({
      turnId: regeneratedTurn.turnId,
      scopeKey: regeneratedTurn.scopeKey,
      content: "",
    });
    s = appendStreamingToken(s, regeneratedTurn.turnId, "fresh ");
    expect(s?.content).toBe("fresh ");
    // The old turn can neither append nor clear the regenerated surface.
    s = appendStreamingToken(s, A.turnId, "stale again");
    s = endStreamingTurn(s, A.turnId);
    expect(s?.content).toBe("fresh ");
  });

  it("stop (abort) still clears the owning turn's surface", () => {
    let s: StreamingScopeState = beginStreamingTurn(A.turnId, A.scopeKey);
    s = appendStreamingToken(s, A.turnId, "partial");
    // User pressed Stop: A is still the active turn — its end clears it.
    const stillActive = { turnId: A.turnId, scopeKey: A.scopeKey };
    expect(isActiveTurn(stillActive, A.turnId)).toBe(true);
    s = endStreamingTurn(s, A.turnId);
    expect(s).toBeNull();
  });

  it("frames before any turn began are dropped, not remembered", () => {
    // A late frame with no owner (surface already cleared): no-op.
    let s: StreamingScopeState = null;
    s = appendStreamingToken(s, A.turnId, "orphan");
    expect(s).toBeNull();
    s = endStreamingTurn(s, A.turnId);
    expect(s).toBeNull();
  });

  it("isActiveTurn distinguishes the owner from superseded turns", () => {
    expect(isActiveTurn(null, A.turnId)).toBe(false); // nothing in flight
    expect(isActiveTurn({ turnId: A.turnId, scopeKey: A.scopeKey }, A.turnId)).toBe(true);
    expect(isActiveTurn({ turnId: B.turnId, scopeKey: B.scopeKey }, A.turnId)).toBe(false);
  });
});
