/**
 * Turn-scoped streaming state machine (M5-001a REV-3, police finding 4).
 *
 * The Ask page streams tokens for ONE turn at a time, and the streaming
 * surface (the in-flight assistant bubble) belongs to the turn that owns it.
 * The original implementation kept a single global `streamingContent` string:
 * a workspace switch mid-generation delivered workspace A's tokens onto
 * workspace B's last historical assistant message.
 *
 * The first rework added turn-id guards at the event sites, but every EXIT
 * path still cleared the shared surface ungated: a superseded turn's
 * completion (error frame, or the function's `finally` block after an
 * abort-on-scope-change) ran `setStreamingContent(null)` / `setPending(false)`
 * / nulled the abort-controller ref and could still clobber the NEW scope's
 * in-flight turn — same defect class, different site.
 *
 * This module is the fence itself. Every mutation of the streaming surface
 * goes through it, and every function is a PURE transition of the
 * (state, turn) pair so the interleavings are testable without a DOM:
 *   - `beginStreamingTurn`  — a new turn takes ownership of the surface;
 *   - `appendStreamingToken` — a token extends ONLY its own turn's content;
 *   - `endStreamingTurn`    — a turn releases ONLY the surface it still owns.
 *
 * A token, an end, or an error belonging to a turn that no longer owns the
 * surface is a no-op: it can neither paint under another scope nor clear
 * another scope's in-flight stream.
 */

/** The in-flight streaming surface, owned by exactly one turn. */
export type StreamingScopeState = {
  /** The turn currently being streamed. */
  turnId: string;
  /** The `account::workspace` scope the turn was started in. */
  scopeKey: string;
  /** The partial answer text accumulated so far. */
  content: string;
} | null;

/** The identity of the turn that currently owns the page's stream machinery. */
export type ActiveTurn = { turnId: string; scopeKey: string } | null;

/**
 * Start a new turn: it takes ownership of the surface with empty content.
 * Any previous owner's state is replaced — the new turn is the only one that
 * can paint or clear the surface from here on.
 */
export function beginStreamingTurn(
  turnId: string,
  scopeKey: string,
): StreamingScopeState {
  return { turnId, scopeKey, content: "" };
}

/**
 * Append one token to its turn's content. A token for a superseded turn
 * (workspace switch, or a stale frame from an aborted stream) returns the
 * state UNCHANGED — it never paints under another scope.
 */
export function appendStreamingToken(
  state: StreamingScopeState,
  turnId: string,
  text: string,
): StreamingScopeState {
  if (state === null || state.turnId !== turnId) return state;
  return { ...state, content: state.content + text };
}

/**
 * Release the surface when a turn finishes. Only the OWNING turn can clear
 * it: a superseded turn's end-of-turn is a no-op so it cannot blank the new
 * scope's in-flight stream.
 */
export function endStreamingTurn(
  state: StreamingScopeState,
  turnId: string,
): StreamingScopeState {
  if (state === null || state.turnId !== turnId) return state;
  return null;
}

/**
 * True when `turnId` is still the turn that owns the page's stream machinery
 * (the pending indicator, the abort controller). A superseded turn's
 * completion must not clear those either — this is the gate for the exit
 * paths that write page-level flags rather than the streaming surface itself.
 */
export function isActiveTurn(active: ActiveTurn, turnId: string): boolean {
  return active !== null && active.turnId === turnId;
}
