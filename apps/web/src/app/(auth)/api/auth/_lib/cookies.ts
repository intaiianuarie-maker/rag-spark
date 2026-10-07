/**
 * Auth cookie policy (server-side only; this proxy owns all Set-Cookie).
 *
 * Tokens are httpOnly and opaque to the browser: JS can never read them
 * (TASK-M1-002a "Out": no tokens in localStorage). Web-side names are
 * deliberately NOT the raw token payloads' claim names.
 *
 * - kios_access: short-lived access token; Max-Age == backend expires_in.
 * - kios_refresh: long-lived refresh token (rotated on every refresh). The
 *   server-side family revocation (backend Redis) is the real TTL authority;
 *   the browser Max-Age only bounds cookie lifetimes between refreshes.
 *
 * COOKIE_SECURE=true turns on `Secure` for deployments behind TLS. It is
 * false by default so the plain-HTTP local compose stack keeps working.
 */

import type { NextResponse } from "next/server";

export const ACCESS_COOKIE = "kios_access";
export const REFRESH_COOKIE = "kios_refresh";

export const REFRESH_COOKIE_MAX_AGE_SECONDS = 14 * 24 * 60 * 60;

export type IssuedTokens = {
  access_token: unknown;
  refresh_token: unknown;
  expires_in: unknown;
};

/** Runtime guard for the backend TokenPair body (frozen M1-001a contract). */
export function parseTokenPair(payload: unknown): IssuedTokens | null {
  if (typeof payload !== "object" || payload === null) return null;
  const record = payload as Record<string, unknown>;
  if (typeof record.access_token !== "string" || record.access_token.length === 0) return null;
  if (typeof record.refresh_token !== "string" || record.refresh_token.length === 0) return null;
  if (typeof record.expires_in !== "number" || !Number.isFinite(record.expires_in) || record.expires_in <= 0) {
    return null;
  }
  return {
    access_token: record.access_token,
    refresh_token: record.refresh_token,
    expires_in: record.expires_in,
  };
}

function cookieOptions(maxAge: number) {
  return {
    httpOnly: true,
    sameSite: "lax" as const,
    path: "/",
    secure: process.env.COOKIE_SECURE === "true",
    maxAge,
  };
}

export function setAuthCookies(
  response: NextResponse,
  tokens: IssuedTokens,
): void {
  const expires = typeof tokens.expires_in === "number" ? tokens.expires_in : 900;

  response.cookies.set(ACCESS_COOKIE, String(tokens.access_token), cookieOptions(expires));
  response.cookies.set(
    REFRESH_COOKIE,
    String(tokens.refresh_token),
    cookieOptions(REFRESH_COOKIE_MAX_AGE_SECONDS),
  );
}

export function clearAuthCookies(response: NextResponse): void {
  response.cookies.set(ACCESS_COOKIE, "", cookieOptions(0));
  response.cookies.set(REFRESH_COOKIE, "", cookieOptions(0));
}
