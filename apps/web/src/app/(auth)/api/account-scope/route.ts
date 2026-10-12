import { cookies } from "next/headers";
import { ACCESS_COOKIE } from "@/app/(auth)/api/auth/_lib/cookies";

/**
 * Account scope for the Ask transcript (T-V1-033 — RP-13/QF-13).
 *
 * `GET /api/account-scope` answers exactly one question for the client-side
 * persistence layer: WHICH ACCOUNT is signed in on this browser, so the Ask
 * history key can be namespaced by account as well as by workspace.
 *
 * Why a route at all: the access token lives in an httpOnly cookie (by
 * design — M1-002a), so page JavaScript cannot read it, and the backend
 * exposes no identity endpoint (checked: `/auth/*` is login/register/refresh/
 * logout only, `/v1/me/*` is invitations only). The identity therefore has to
 * be read server-side, next to the cookie.
 *
 * Security contract (read before changing this):
 *  - the `sub` claim is decoded WITHOUT verifying the signature, and it is
 *    used ONLY as a namespace for a browser-local cache. It is never an
 *    authorization input: every read of the underlying content still goes to
 *    the backend with the bearer token, which verifies the signature (R-004).
 *  - forging `sub` requires writing an httpOnly cookie for this origin,
 *    which already implies control of the browser profile that owns the
 *    localStorage the cache lives in — no new attack surface.
 *  - a missing/garbled/non-access token yields `scope: null`, and the client
 *    then persists NOTHING rather than falling back to a shared namespace.
 */

/** Ceiling on a decoded scope value (protects the storage key + payload). */
export const MAX_SCOPE_LENGTH = 128;

/** Scope must be usable as a storage-key segment; see `ask/history.ts`. */
const SCOPE_PART = /^[A-Za-z0-9._@+-]{1,128}$/;

function base64UrlDecode(segment: string): string | null {
  try {
    return Buffer.from(segment, "base64url").toString("utf8");
  } catch {
    return null;
  }
}

/**
 * Account scope from an access token, or null when it cannot be trusted to
 * name a namespace. Accepts only a 3-segment JWT whose payload carries a
 * `sub` we can write into a key; a `type` claim other than `access` is
 * rejected so a refresh token can never be mistaken for identity.
 */
export function decodeAccountScope(token: string | null | undefined): string | null {
  if (typeof token !== "string" || token.length === 0) return null;
  const segments = token.split(".");
  if (segments.length !== 3) return null;
  const payload = base64UrlDecode(segments[1]);
  if (payload === null) return null;

  let claims: unknown;
  try {
    claims = JSON.parse(payload);
  } catch {
    return null;
  }
  if (typeof claims !== "object" || claims === null || Array.isArray(claims)) return null;
  const record = claims as Record<string, unknown>;

  const sub = record.sub;
  if (typeof sub !== "string" || sub.length === 0 || sub.length > MAX_SCOPE_LENGTH) {
    return null;
  }
  if (record.type !== undefined && record.type !== "access") return null;
  if (!SCOPE_PART.test(sub)) return null;
  return sub;
}

export async function GET(): Promise<Response> {
  const jar = await cookies();
  const scope = decodeAccountScope(jar.get(ACCESS_COOKIE)?.value);
  return Response.json(
    { ok: true, scope },
    { status: 200, headers: { "cache-control": "no-store" } },
  );
}
