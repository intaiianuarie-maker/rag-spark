"use client";

/**
 * Typed auth client for apps/web (TASK-M1-002a).
 *
 * Frozen contract (M1-001a + boss rulings in docs/engineering/frontend-m1-prep.md):
 *  - the browser only talks to same-origin routes: /api/auth/* (cookie owners)
 *    and /api/backend/<path> (generic proxy attaching the Bearer cookie);
 *  - tokens live in httpOnly cookies — never in localStorage;
 *  - expiry handling: 401 on an app call → single-flight refresh → retry the
 *    call exactly once; if the refresh fails → redirect to /login?next=…;
 *    after re-login the page reissues its calls (resume via ?next), so no
 *    replay bookkeeping is needed inside the client.
 *
 * `createAuthClient` injects fetch/navigator/current-path for unit tests; the
 * default `auth` instance is what app code imports.
 */

export type AuthErrorCode =
  | "invalid_credentials"
  | "invalid_token"
  | "token_expired"
  | "rate_limited"
  | "email_taken"
  | "auth_unavailable"
  | "validation_error"
  | "backend_unreachable"
  | "unexpected_response"
  | "request_failed"
  | "network_error";

const KNOWN_ERROR_CODES: readonly AuthErrorCode[] = [
  "invalid_credentials",
  "invalid_token",
  "token_expired",
  "rate_limited",
  "email_taken",
  "auth_unavailable",
  "validation_error",
  "backend_unreachable",
  "unexpected_response",
  "request_failed",
  "network_error",
];

export type AuthFailure = {
  code: AuthErrorCode;
  message: string;
  status: number;
  /** Present on 429 (seconds). */
  retryAfter?: number;
  /** Present on validation_error: field name → messages. */
  fields?: Record<string, string[]>;
};

export type AuthResult = { ok: true } | { ok: false; failure: AuthFailure };

/** Thrown by `authFetch` when the session cannot be restored automatically. */
export class AuthLoginRequiredError extends Error {
  readonly loginUrl: string;

  constructor(loginUrl: string) {
    super("Authentication required");
    this.name = "AuthLoginRequiredError";
    this.loginUrl = loginUrl;
  }
}

/** Thrown for non-2xx backend responses that are not auth-redirects. */
export class AuthApiError extends Error {
  readonly status: number;
  readonly code: AuthErrorCode;

  constructor(status: number, code: AuthErrorCode, message: string) {
    super(message);
    this.name = "AuthApiError";
    this.status = status;
    this.code = code;
  }
}

export type AuthClientDeps = {
  /** Injectable fetch for tests (defaults to global fetch). */
  fetcher?: typeof fetch;
  /** Injectable navigation for tests (defaults to window.location.assign). */
  navigate?: (loginUrl: string) => void;
  /** Injectable current path for tests (defaults to location pathname+search). */
  getCurrentPath?: () => string;
};

export type AuthFetchInit = {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  headers?: Record<string, string>;
  /**
   * "auto-redirect" (default): when the session cannot be restored, navigate
   * to /login?next=… and throw AuthLoginRequiredError.
   * "strict": never navigate — callers that handle anonymous state themselves
   * (e.g. the workspace provider on public pages) catch the error instead.
   */
  authMode?: "auto-redirect" | "strict";
};

export function createAuthClient(deps?: AuthClientDeps) {
  const fetcher = deps?.fetcher ?? fetch.bind(globalThis);
  const navigate =
    deps?.navigate ??
    ((loginUrl: string) => {
      if (typeof window !== "undefined") window.location.assign(loginUrl);
    });
  const getCurrentPath =
    deps?.getCurrentPath ??
    (() =>
      typeof window === "undefined"
        ? "/"
        : `${window.location.pathname}${window.location.search}`);

  let refreshInFlight: Promise<boolean> | null = null;

  function loginUrlForCurrent(): string {
    return `/login?next=${encodeURIComponent(getCurrentPath())}`;
  }

  async function readJson(response: Response): Promise<unknown> {
    try {
      const text = await response.text();
      if (text.trim().length === 0) return null;
      return JSON.parse(text);
    } catch {
      return null;
    }
  }

  /** Never throws on network errors: synthesizes a 502 proxy-shaped body so
   * every code path below deals with a consistent Response. */
  async function safeFetch(url: string, init: RequestInit): Promise<Response> {
    try {
      return await fetcher(url, init);
    } catch {
      return new Response(
        JSON.stringify({
          ok: false,
          error: { code: "backend_unreachable", message: "Network error. Please try again." },
        }),
        { status: 502, headers: { "content-type": "application/json" } },
      );
    }
  }

  /**
   * Parses a failure payload into AuthFailure. Understands the proxy shape
   * ({ok:false, error:{code,message}, retryAfter?, fields?}), the nested
   * backend shape ({"detail":{"error","message"}}), and a flat fallback.
   */
  function parseFailure(payload: unknown, status: number): AuthFailure {
    const record =
      typeof payload === "object" && payload !== null
        ? (payload as Record<string, unknown>)
        : ({} as Record<string, unknown>);

    let code = "";
    let message = "";

    const error = record.error;
    if (error !== null && typeof error === "object") {
      const nestedError = error as Record<string, unknown>;
      if (typeof nestedError.code === "string") code = nestedError.code;
      if (typeof nestedError.message === "string") message = nestedError.message;
    } else if (typeof error === "string") {
      code = error;
    }

    if (typeof record.detail === "object" && record.detail !== null) {
      const nestedDetail = record.detail as Record<string, unknown>;
      if (code === "" && typeof nestedDetail.error === "string") code = nestedDetail.error;
      if (message === "" && typeof nestedDetail.message === "string") message = nestedDetail.message;
    }
    if (code === "" && typeof record.error === "string") code = record.error;
    if (message === "" && typeof record.message === "string") message = record.message;

    if (code === "") code = "request_failed";
    if (message === "") message = "Request failed. Please try again.";

    const normalizedCode = (KNOWN_ERROR_CODES as readonly string[]).includes(code)
      ? (code as AuthErrorCode)
      : "request_failed";

    const failure: AuthFailure = { code: normalizedCode, message, status };

    // Field errors, two sources:
    //  1) proxy-mapped {"fields": {...}} (auth proxy), or
    //  2) raw backend 422 passthrough (via /api/backend): map the detail
    //     issues array by the last `loc` segment → field name.
    if (record.fields !== null && typeof record.fields === "object") {
      const fields: Record<string, string[]> = {};
      for (const [field, value] of Object.entries(record.fields as Record<string, unknown>)) {
        if (Array.isArray(value) && value.every((item) => typeof item === "string")) {
          fields[field] = value;
        }
      }
      if (Object.keys(fields).length > 0) failure.fields = fields;
    }

    if (failure.fields === undefined && Array.isArray(record.detail)) {
      const rawFields: Record<string, string[]> = {};
      for (const issue of record.detail) {
        if (typeof issue !== "object" || issue === null) continue;
        const item = issue as { loc?: unknown; msg?: unknown };
        const loc = Array.isArray(item.loc) ? item.loc : [];
        if (loc.length === 0) continue;
        const field = String(loc[loc.length - 1]);
        if (field === "body" || field === "detail") continue;
        const text = typeof item.msg === "string" ? item.msg : "Invalid value.";
        (rawFields[field] ??= []).push(text);
      }
      if (Object.keys(rawFields).length > 0) failure.fields = rawFields;
    }

    if (typeof record.retryAfter === "number" && Number.isFinite(record.retryAfter) && record.retryAfter >= 0) {
      failure.retryAfter = record.retryAfter;
    }
    return failure;
  }

  /** Single-flight session refresh: concurrent callers share one rotation. */
  async function refreshNow(): Promise<boolean> {
    if (refreshInFlight !== null) return refreshInFlight;
    const attempt = (async () => {
      try {
        const response = await fetcher("/api/auth/refresh", {
          method: "POST",
          cache: "no-store",
          headers: { accept: "application/json" },
        });
        if (response.status !== 200) return false;
        const payload = await readJson(response);
        return (payload as { ok?: unknown } | null)?.ok === true;
      } catch {
        return false;
      }
    })();
    refreshInFlight = attempt;
    try {
      return await attempt;
    } finally {
      // Clear so later, sequential callers may rotate again. Rotation always
      // consumes the latest refresh cookie, so sequential refreshes are safe;
      // single-flight only prevents concurrent replays of the same token.
      if (refreshInFlight === attempt) refreshInFlight = null;
    }
  }

  async function authPost(path: string, body: unknown): Promise<AuthResult> {
    const response = await safeFetch(path, {
      method: "POST",
      cache: "no-store",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    if (response.status === 200) return { ok: true };
    return { ok: false, failure: parseFailure(await readJson(response), response.status) };
  }

  async function login(email: string, password: string): Promise<AuthResult> {
    return authPost("/api/auth/login", { email, password });
  }

  async function register(email: string, password: string): Promise<AuthResult> {
    return authPost("/api/auth/register", { email, password });
  }

  async function logout(): Promise<AuthResult> {
    return authPost("/api/auth/logout", {});
  }

  /**
   * App-API call through /api/backend/<path> with the Bearer cookie attached
   * server-side. Transparent refresh: one 401 → single-flight refresh → one
   * retry; refresh failure → redirect to /login?next=… (resume via ?next).
   */
  async function authFetch<T = unknown>(path: string, init?: AuthFetchInit): Promise<T> {
    const method = init?.method ?? "GET";
    const joined = path.replace(/^\/+/, "");

    async function call(): Promise<Response> {
      const headers: Record<string, string> = {
        accept: "application/json",
        ...(init?.headers ?? {}),
      };
      let requestBody: string | undefined;
      if (method !== "GET" && init?.body !== undefined) {
        headers["content-type"] = "application/json";
        requestBody = JSON.stringify(init.body);
      }
      return safeFetch(`/api/backend/${joined}`, {
        method,
        cache: "no-store",
        headers,
        body: requestBody,
      });
    }

    let response = await call();
    if (response.status === 401) {
      const refreshed = await refreshNow();
      const loginUrl = loginUrlForCurrent();
      if (!refreshed) {
        if (init?.authMode !== "strict") navigate(loginUrl);
        throw new AuthLoginRequiredError(loginUrl);
      }
      response = await call();
      if (response.status === 401) {
        // Even a just-refreshed access token says no: degrade to the login
        // flow once, without looping (e.g. Forbidden semantics on the
        // upstream endpoint; ticket b/c pay attention to 401 vs 403).
        if (init?.authMode !== "strict") navigate(loginUrl);
        throw new AuthLoginRequiredError(loginUrl);
      }
    }
    if (!response.ok) {
      const failure = parseFailure(await readJson(response), response.status);
      throw new AuthApiError(failure.status, failure.code, failure.message);
    }
    return (await readJson(response)) as T;
  }

  return { login, register, logout, refreshNow, authFetch };
}

/** Default shared client. */
export const auth = createAuthClient();

/** Server-safe pure helpers live in `auth-client.server.ts` (server
 * components cannot call into a "use client" module); re-exported here for
 * client-side forms. */
export { sanitizeNextPath } from "./auth-client.server";
