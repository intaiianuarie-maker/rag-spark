/**
 * Isomorphic (server-safe) helpers for the auth pages.
 *
 * Deliberately separate from `auth-client.ts` (a "use client" module):
 * server components cannot call functions defined in a client module, so
 * the pure validators live here and are re-exported by the client module for
 * forms. No imports beyond the standard library keep it server-callable.
 */

/** Validates a `?next=`/redirect target is same-origin (no protocol, no
 * protocol-relative host) and not another auth page. `null` when unsafe. */
export function sanitizeNextPath(value: string | undefined | null): string | null {
  if (typeof value !== "string" || value.length === 0) return null;
  if (!value.startsWith("/")) return null;
  if (value.startsWith("//")) return null;
  if (value.includes("://")) return null;
  if (value.startsWith("/\\")) return null;
  const firstSegment = value.slice(1).split(/[/?]/)[0];
  if (firstSegment === "login" || firstSegment === "register") return null;
  return value;
}
