/**
 * T-V1-033 — account scope route (RP-13/QF-13).
 *
 * This route is what makes the history key account-scoped: page JavaScript
 * cannot read the httpOnly access cookie, and the backend exposes no
 * identity endpoint, so the account name for the storage namespace is read
 * here — server-side, next to the cookie, and nowhere else.
 *
 * The contract under test: a well-formed access token yields its `sub`, and
 * EVERY other input (missing, garbled, non-access, key-unsafe) yields null —
 * never a shared fallback namespace.
 */
import { describe, expect, it, vi } from "vitest";

const cookieGetMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: cookieGetMock })),
}));

import { ACCESS_COOKIE } from "../auth/_lib/cookies";
import { GET, MAX_SCOPE_LENGTH, decodeAccountScope } from "./route";

const ACCOUNT = "33333333-3333-4333-8333-333333333333";

function token(claims: Record<string, unknown>): string {
  const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString("base64url");
  return `${encode({ alg: "HS256", typ: "JWT" })}.${encode(claims)}.c2lnbmF0dXJl`;
}

describe("decodeAccountScope", () => {
  it("names the account of a real access token", () => {
    expect(decodeAccountScope(token({ sub: ACCOUNT, type: "access", jti: "j" }))).toBe(ACCOUNT);
  });

  it("accepts an email-style sub (ids are not always uuids)", () => {
    expect(decodeAccountScope(token({ sub: "dev+1@example.com", type: "access" }))).toBe(
      "dev+1@example.com",
    );
  });

  it("rejects a refresh token so a session credential never becomes a namespace", () => {
    expect(decodeAccountScope(token({ sub: ACCOUNT, type: "refresh" }))).toBeNull();
  });

  it("rejects missing, malformed and non-JWT inputs", () => {
    expect(decodeAccountScope(undefined)).toBeNull();
    expect(decodeAccountScope(null)).toBeNull();
    expect(decodeAccountScope("")).toBeNull();
    expect(decodeAccountScope("not-a-token")).toBeNull();
    expect(decodeAccountScope("only.two")).toBeNull();
    expect(decodeAccountScope(`head.${Buffer.from("{not json").toString("base64url")}.sig`)).toBeNull();
    expect(decodeAccountScope(`head.${Buffer.from("[1,2]").toString("base64url")}.sig`)).toBeNull();
    expect(decodeAccountScope(token({ type: "access" }))).toBeNull();
    expect(decodeAccountScope(token({ sub: "", type: "access" }))).toBeNull();
    expect(decodeAccountScope(token({ sub: 42, type: "access" }))).toBeNull();
  });

  it("rejects a sub that cannot be written into a storage key", () => {
    expect(decodeAccountScope(token({ sub: "a:b", type: "access" }))).toBeNull();
    expect(decodeAccountScope(token({ sub: "acct/../other", type: "access" }))).toBeNull();
    expect(decodeAccountScope(token({ sub: "has space", type: "access" }))).toBeNull();
    expect(
      decodeAccountScope(token({ sub: "x".repeat(MAX_SCOPE_LENGTH + 1), type: "access" })),
    ).toBeNull();
  });
});

describe("GET /api/account-scope", () => {
  it("answers the scope from the access cookie and never caches", async () => {
    cookieGetMock.mockReturnValue({ name: ACCESS_COOKIE, value: token({ sub: ACCOUNT, type: "access" }) });

    const response = await GET();

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(await response.json()).toEqual({ ok: true, scope: ACCOUNT });
  });

  it("answers scope:null (not a shared fallback) with no cookie", async () => {
    cookieGetMock.mockReturnValue(undefined);

    const response = await GET();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ ok: true, scope: null });
  });

  it("answers scope:null for a cookie it cannot interpret", async () => {
    cookieGetMock.mockReturnValue({ name: ACCESS_COOKIE, value: "garbage" });

    const response = await GET();

    expect(await response.json()).toEqual({ ok: true, scope: null });
  });
});
