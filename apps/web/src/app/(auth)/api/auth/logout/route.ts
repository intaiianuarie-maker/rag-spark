import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { extractBackendError, postToAuthBackend } from "../_lib/backend";
import { clearAuthCookies, REFRESH_COOKIE } from "../_lib/cookies";

/**
 * Same-origin logout proxy: forwards the refresh token for server-side family
 * revocation, then clears our cookies. Idempotent semantics preserved from
 * upstream: an invalid/expired token still results in a local logout.
 *
 * Cookie lifetime is only cleared on success-ish outcomes (2xx/401/422);
 * if the backend is unreachable the cookies are kept so the browser can retry
 * logout instead of leaking an otherwise-valid refresh family.
 */
export async function POST(): Promise<Response> {
  const jar = await cookies();
  const refreshToken = jar.get(REFRESH_COOKIE)?.value ?? null;

  if (refreshToken === null) {
    const response = NextResponse.json({ ok: true });
    clearAuthCookies(response);
    return response;
  }

  const outcome = await postToAuthBackend("/auth/logout", { refresh_token: refreshToken });
  if (outcome.kind === "unreachable") {
    return NextResponse.json(
      {
        ok: false,
        error: {
          code: "backend_unreachable",
          message: "Could not reach the auth service to revoke the session. Try again.",
        },
      },
      { status: 502 },
    );
  }

  const response = NextResponse.json({ ok: true });
  if (outcome.status === 204 || outcome.status === 401 || outcome.status === 422) {
    clearAuthCookies(response);
    return response;
  }

  // Unexpected upstream status: report it, keep the cookie for a retry.
  const extracted = extractBackendError(outcome.body);
  return NextResponse.json(
    { ok: false, error: { code: extracted.code, message: extracted.message } },
    { status: outcome.status },
  );
}
