/**
 * Initial-URL-search gate (M5-001b REV-2, police finding 7).
 *
 * The search page derives its first search from the URL (?q=…&mode=…&filters),
 * but `WorkspaceProvider` resolves the active workspace ASYNCHRONOUSLY: the
 * mount-only effect ran while `active === null`, `runSearch` returned
 * immediately, and the effect never re-ran when the workspace finally
 * resolved — a reload or pasted URL silently did nothing.
 *
 * The gate is the decision half of the fix, extracted as pure transitions so
 * the delayed-initialization interleaving (and the REV-1 idempotency
 * requirement — a workspace SWITCH must not re-derive a search from the URL)
 * are exercised for real, not re-read from source.
 */

/** One-shot latch for the initial URL-derived search, per mount. */
export type InitialUrlGate = { ran: boolean };

export function createInitialUrlGate(): InitialUrlGate {
  return { ran: false };
}

/**
 * True exactly once per mount, at the first moment the active workspace is
 * available. Before the provider resolves (`activeId === null`) the gate is
 * closed; after it has fired it stays closed — so a workspace switch never
 * re-derives a search from the URL.
 */
export function shouldRunInitialUrlSearch(
  gate: InitialUrlGate,
  activeId: string | null | undefined,
): boolean {
  // The page derives the workspace id as `active?.id`, which is `undefined`
  // while WorkspaceProvider has `active = null` — `null` is NOT the only
  // absent value. Rejecting only `null` opened the gate before readiness, it
  // latched, and the search that fired hit `runSearch`'s own no-workspace
  // return, so the later workspace resolution could never search (POLICE
  // REV-3 residual: zero requests after readiness).
  if (activeId === null || activeId === undefined) return false;
  if (gate.ran) return false; // idempotent: fires once per mount
  return true;
}

/** Latch the gate after the initial search has been dispatched. */
export function markInitialUrlSearchRan(gate: InitialUrlGate): void {
  gate.ran = true;
}
