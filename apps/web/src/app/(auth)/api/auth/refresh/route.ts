import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import {
  extractBackendError,
  postToAuthBackend,
} from "../_lib/backend";
import { clearAuthCookies, REFRESH_COOKIE, parseTokenPair, setAuthCookies } from "../_lib/cookies";

/**
 * Same-origin refresh proxy. Reads the refresh cookie (never sent by JS),
 * rotates it upstream, and sets the rotated cookie pair on success. On any
 * auth failure both cookies are cleared so the client cleanly enters the
 * re-login flow (auth-client single-flight refresh is built on this route).
 */
export async function POST(): Promise<Response> {
  const jar = await cookies();
  const refreshToken = jar.get(REFRESH_COOKIE)?.value ?? null;

  if (refreshToken === null) {
    const response = NextResponse.json(
      {
        ok: false,
        error: { code: "invalid_token", message: "Session expired. Please sign in." },
      },
      { status: 401 },
    );
    clearAuthCookies(response);
    return response;
  }

  const outcome = await postToAuthBackend("/auth/refresh", { refresh_token: refreshToken });
  if (outcome.kind === "unreachable") {
    // Backend down: keep cookies so a retry of the same refresh can succeed
    // (rotation only happened if upstream processed it).
    return NextResponse.json(
      {
        ok: false,
        error: {
          code: "backend_unreachable",
          message: "Session refresh failed because the auth service is unreachable. Please retry.",
        },
      },
      { status: 502 },
    );
  }

  if (outcome.status === 200) {
    const tokens = parseTokenPair(outcome.body);
    if (tokens !== null) {
      const response = NextResponse.json({ ok: true });
      setAuthCookies(response, tokens);
      return response;
    }
  }

  // 4xx = definitive auth answer (expired/invalid/tampered): clear cookies so
  // the next attempt does not replay a dead token. 5xx answers are transient
  // (e.g. auth store unavailable): keep the cookie so a later retry can
  // succeed once the backend recovers.
  const definitiveFailure = outcome.status >= 400 && outcome.status < 500;
  const response = NextResponse.json(
    {
      ok: false,
      error: (() => {
        const extracted = extractBackendError(outcome.body);
        return { code: extracted.code, message: extracted.message };
      })(),
    },
    { status: outcome.status === 200 ? 502 : outcome.status },
  );
  if (definitiveFailure || outcome.status === 200) {
    clearAuthCookies(response);
  }
  return response;
}
