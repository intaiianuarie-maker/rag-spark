/**
 * Unit tests for the auth client (TASK-M1-002a): 401 mapping, refresh
 * single-flight, error mapping, resume redirect. No jsdom needed: everything
 * runs through an injectable fetcher/navigator.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  AuthApiError,
  AuthLoginRequiredError,
  createAuthClient,
  sanitizeNextPath,
  type AuthFetchInit,
} from "./auth-client";

type RecordedCall = { url: string; init: RequestInit };

/** Scripted fetcher: responses are consumed in order, per URL selector. */
function scriptedFetcher(
  script: Array<{ match: string; respond: () => Response | number | never }>,
): { fetcher: typeof fetch; calls: RecordedCall[]; refreshCalls: () => number } {
  const calls: RecordedCall[] = [];
  const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init: init ?? {} });
    for (const entry of script) {
      if (url.includes(entry.match)) {
        const value = entry.respond();
        if (typeof value === "number") return new Response(null, { status: value });
        if (value instanceof Response) return value;
        return new Response(null, { status: 500 });
      }
    }
    return new Response(null, { status: 404 });
  }) as unknown as typeof fetch;
  const refreshCalls = () => calls.filter((call) => call.url.includes("/api/auth/refresh")).length;
  return { fetcher, calls, refreshCalls };
}

function jsonResponse(body: unknown, status = 200, headers?: HeadersInit): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...(headers ?? {}) },
  });
}

async function drive<T>(promise: Promise<T>): Promise<void> {
  await promise;
}

describe("login/register/logout result mapping", () => {
  it("maps a 200 login to ok", async () => {
    const { fetcher } = scriptedFetcher([
      { match: "/api/auth/login", respond: () => jsonResponse({ ok: true }, 200) },
    ]);
    const client = createAuthClient({ fetcher });
    const result = await client.login("user@example.com", "pw");
    expect(result).toEqual({ ok: true });
  });

  it("maps a nested 401 (frozen backend shape) to invalid_credentials + generic message", async () => {
    const { fetcher } = scriptedFetcher([
      {
        match: "/api/auth/login",
        respond: () =>
          jsonResponse(
            { detail: { error: "invalid_credentials", message: "Invalid email or password." } },
            401,
          ),
      },
    ]);
    const client = createAuthClient({ fetcher });
    const result = await client.login("user@example.com", "wrong");
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.failure.code).toBe("invalid_credentials");
      expect(result.failure.message).toBe("Invalid email or password.");
      expect(result.failure.status).toBe(401);
      expect(result.failure.fields).toBeUndefined();
    }
  });

  it("maps 429 with proxy retryAfter (rate_limited)", async () => {
    const { fetcher } = scriptedFetcher([
      {
        match: "/api/auth/login",
        respond: () =>
          jsonResponse(
            { detail: { error: "rate_limited", message: "Too many login attempts. Try again later." } },
            429,
          ),
      },
    ]);
    const client = createAuthClient({ fetcher });
    const result = await client.login("user@example.com", "pw");
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.failure.code).toBe("rate_limited");
    }
  });

  it("maps 429 proxy-body retryAfter into the failure", async () => {
    const { fetcher } = scriptedFetcher([
      {
        match: "/api/auth/login",
        respond: () =>
          jsonResponse(
            {
              ok: false,
              error: { code: "rate_limited", message: "Too many attempts." },
              retryAfter: 42,
            },
            429,
          ),
      },
    ]);
    const client = createAuthClient({ fetcher });
    const result = await client.login("user@example.com", "pw");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.failure.retryAfter).toBe(42);
  });

  it("maps register 409 to email_taken", async () => {
    const { fetcher } = scriptedFetcher([
      {
        match: "/api/auth/register",
        respond: () =>
          jsonResponse(
            {
              ok: false,
              error: { code: "email_taken", message: "Unable to register with the provided email." },
            },
            409,
          ),
      },
    ]);
    const client = createAuthClient({ fetcher });
    const result = await client.register("duplicate@example.com", "password1");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.failure.code).toBe("email_taken");
  });

  it("maps validation errors with inline fields (password length)", async () => {
    const { fetcher } = scriptedFetcher([
      {
        match: "/api/auth/register",
        respond: () =>
          jsonResponse(
            {
              detail: [
                { type: "string_too_short", loc: ["body", "password"], msg: "String should have at least 8 characters" },
                { type: "value_error", loc: ["body", "email"], msg: "invalid email address" },
              ],
              error: "validation_error",
              message: "Please review the highlighted fields.",
            },
            422,
          ),
      },
    ]);
    const client = createAuthClient({ fetcher });
    const result = await client.register("nope", "short");
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.failure.code).toBe("validation_error");
      expect(result.failure.fields?.password).toHaveLength(1);
      expect(result.failure.fields?.email).toHaveLength(1);
    }
  });

  it("normalizes a network failure to a 502 failure", async () => {
    const { fetcher } = scriptedFetcher([{ match: "/api/auth/login", respond: () => { throw new TypeError("fetch failed"); } }]);
    const client = createAuthClient({ fetcher });
    const result = await client.login("user@example.com", "pw");
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.failure.code).toBe("backend_unreachable");
      expect(result.failure.status).toBe(502);
    }
  });

  it("logout reports ok on 204-shaped empty success from proxy", async () => {
    const { fetcher } = scriptedFetcher([
      { match: "/api/auth/logout", respond: () => jsonResponse({ ok: true }, 200) },
    ]);
    const client = createAuthClient({ fetcher });
    expect(await client.logout()).toEqual({ ok: true });
  });
});

describe("snapping the refresh flow onto app calls", () => {
  it("retries once after a successful refresh (401 → refresh → 200)", async () => {
    let proxyStatus = 401;
    const { fetcher, calls, refreshCalls } = scriptedFetcher([
      {
        match: "/api/backend",
        respond: () => {
          const status = proxyStatus === 401 ? 401 : 200;
          proxyStatus = 200;
          return status === 401 ? new Response(null, { status: 401 }) : jsonResponse({ value: "data" });
        },
      },
      {
        match: "/api/auth/refresh",
        respond: () => jsonResponse({ ok: true }, 200),
      },
    ]);
    const client = createAuthClient({ fetcher, navigate: () => {} });
    const result = await client.authFetch<{ value: string }>("/v1/workspaces");
    expect(result.value).toBe("data");
    expect(refreshCalls()).toBe(1);
    expect(calls.filter((c) => c.url.includes("/api/backend")).length).toBe(2);
  });

  it("single-flights one refresh across two concurrent 401s", async () => {
    let proxyStatus = 401;
    const { fetcher, refreshCalls } = scriptedFetcher([
      {
        match: "/api/backend",
        respond: () => {
          const value = proxyStatus;
          return value === 401 ? new Response(null, { status: 401 }) : jsonResponse({ ok: true });
        },
      },
      {
        match: "/api/auth/refresh",
        respond: () => {
          // After the first rotation the next proxy call succeeds.
          proxyStatus = 200;
          return jsonResponse({ ok: true }, 200);
        },
      },
    ]);
    const client = createAuthClient({ fetcher, navigate: () => {} });
    const [a, b] = await Promise.all([
      client.authFetch("/v1/anything?a"),
      client.authFetch("/v1/anything?b"),
    ]);
    void a;
    void b;
    expect(refreshCalls()).toBe(1);
  });

  it("redirects to /login?next=… when the refresh fails", async () => {
    const { fetcher } = scriptedFetcher([
      { match: "/api/backend", respond: () => new Response(null, { status: 401 }) },
      { match: "/api/auth/refresh", respond: () => jsonResponse({ ok: false, error: { code: "invalid_token", message: "Session expired. Please sign in." } }, 401) },
    ]);
    let navigatedTo: string | null = null;
    const client = createAuthClient({
      fetcher,
      navigate: (url) => {
        navigatedTo = url;
      },
      getCurrentPath: () => "/settings/keys?p=1",
    });
    await expect(
      drive(client.authFetch("/v1/workspaces")),
    ).rejects.toBeInstanceOf(AuthLoginRequiredError);
    expect(navigatedTo).toBe(`/login?next=${encodeURIComponent("/settings/keys?p=1")}`);
  });

  it("does not loop: second 401 after refresh still redirects instead of refreshing again", async () => {
    const { fetcher, refreshCalls } = scriptedFetcher([
      { match: "/api/backend", respond: () => new Response(null, { status: 401 }) },
      { match: "/api/auth/refresh", respond: () => jsonResponse({ ok: true }, 200) },
    ]);
    let navigatedTo: string | null = null;
    const client = createAuthClient({ fetcher, navigate: (url) => (navigatedTo = url) });
    await expect(
      drive(client.authFetch("/v1/workspaces")),
    ).rejects.toBeInstanceOf(AuthLoginRequiredError);
    expect(refreshCalls()).toBe(1);
    expect(navigatedTo).not.toBeNull();
    void navigatedTo;
  });

  it("throws AuthApiError with backend code for non-auth failures (e.g. 404)", async () => {
    const { fetcher } = scriptedFetcher([
      {
        match: "/api/backend",
        respond: () => jsonResponse({ detail: { error: "not_found", message: "No such thing." } }, 404),
      },
    ]);
    const client = createAuthClient({ fetcher, navigate: () => {} });
    await expect(drive(client.authFetch("/v1/missing"))).rejects.toMatchObject({
      status: 404,
    });
    void AuthApiError;
  });

  it("authFetch sends GET without body and POST with JSON body", async () => {
    const { fetcher, calls } = scriptedFetcher([
      { match: "/api/backend", respond: () => jsonResponse({ ok: true }) },
    ]);
    const client = createAuthClient({ fetcher, navigate: () => {} });
    await client.authFetch("/thing");
    const init: AuthFetchInit = { method: "POST", body: { a: 1 } };
    await client.authFetch("/thing", init);
    const [first, second] = calls;
    expect(String(first.url)).toBe("/api/backend/thing");
    expect(String(second.url)).toBe("/api/backend/thing");
    expect((second.init as Record<string, unknown>).body).toBe(JSON.stringify({ a: 1 }));
  });
});

/**
 * Serialization boundary contract (T-V1-010 · RP-06/RP-07 · QF-06/QF-07).
 * These pin the ONE object→wire decision in auth-client: objects encoded
 * exactly once, FormData/binary passed through by reference, and zero
 * "does this string already look like JSON?" sniffing.
 */
describe("serialization boundary (T-V1-010)", () => {
  /** Fresh client + the calls its scripted fetcher recorded. */
  function harness(respond: () => Response = () => jsonResponse({ ok: true })) {
    const { fetcher, calls, refreshCalls } = scriptedFetcher([
      { match: "/api/backend", respond },
      { match: "/api/auth/refresh", respond: () => jsonResponse({ ok: true }, 200) },
    ]);
    const client = createAuthClient({ fetcher, navigate: () => {} });
    const backendCalls = () => calls.filter((call) => call.url.includes("/api/backend"));
    return { client, calls, backendCalls, refreshCalls };
  }

  function headerOf(init: RequestInit, name: string): string | null {
    return new Headers(init.headers).get(name);
  }

  it("object-in → object-out: a plain object is JSON-encoded exactly once", async () => {
    const { client, backendCalls } = harness();
    const body = { query: "what is RP-06?", nested: { a: [1, 2] }, flag: true };
    await client.authFetch("/v1/ask", { method: "POST", body });

    const init = backendCalls()[0].init;
    expect(typeof init.body).toBe("string"); // wire form is text, not a live object
    const decoded: unknown = JSON.parse(init.body as string);
    expect(decoded).toEqual(body); // object-in → object-out (lossless round trip)
    expect(decoded).not.toBeTypeOf("string"); // exactly ONCE: double encoding parses back to a string
    expect(headerOf(init, "content-type")).toBe("application/json");
  });

  it("FormData passes through untouched: same instance, no content-type added", async () => {
    const { client, backendCalls } = harness();
    const form = new FormData();
    const file = new File([new Uint8Array([0x00, 0xff, 0xfe, 0x80])], "hostile.bin");
    form.append("file", file);
    form.append("workspace_id", "ws-1");

    await client.authFetch("/v1/documents", { method: "POST", body: form });

    const init = backendCalls()[0].init;
    expect(init.body).toBe(form); // identity: not cloned, not stringified, not re-encoded
    expect(headerOf(init, "content-type")).toBeNull(); // boundary=… stays runtime-generated
    expect((init.body as FormData).get("workspace_id")).toBe("ws-1");
    expect((init.body as FormData).get("file")).toBe(file); // multipart parts untouched
  });

  it("binary bodies (ArrayBuffer / TypedArray) pass through untouched", async () => {
    const { client, backendCalls } = harness();
    const buffer = new Uint8Array([0xde, 0xad, 0xbe, 0xef]).buffer;
    const bytes = new Uint8Array([1, 2, 3]);

    await client.authFetch("/v1/raw-a", { method: "POST", body: buffer });
    await client.authFetch("/v1/raw-b", { method: "POST", body: bytes });

    const [first, second] = backendCalls();
    expect(first.init.body).toBe(buffer);
    expect(second.init.body).toBe(bytes);
    expect(headerOf(first.init, "content-type")).toBeNull();
    expect(headerOf(second.init, "content-type")).toBeNull();
    expect(Array.from(new Uint8Array(first.init.body as ArrayBuffer))).toEqual([
      0xde, 0xad, 0xbe, 0xef,
    ]);
  });

  it("no string sniffing: JSON-looking text is sent byte-identical", async () => {
    const { client, backendCalls } = harness();
    const preJson = '{"already":"json","n":1}';

    await client.authFetch("/v1/a", { method: "POST", body: preJson });

    const init = backendCalls()[0].init;
    expect(init.body).toBe(preJson); // not re-quoted, not parsed-and-restringified
    expect(headerOf(init, "content-type")).toBeNull(); // no guess about what the string means

    const { client: secondClient, backendCalls: secondCalls } = harness();
    await secondClient.authFetch("/v1/b", { method: "POST", body: "plain text" });
    const plain = secondCalls()[0].init;
    expect(plain.body).toBe("plain text"); // verbatim, not '"plain text"'
    expect(headerOf(plain, "content-type")).toBeNull();
  });

  it("a JSON-looking string nested in an object stays data, encoded once", async () => {
    const { client, backendCalls } = harness();
    await client.authFetch("/v1/c", { method: "POST", body: { raw: '{"a":1}' } });

    const init = backendCalls()[0].init;
    expect(JSON.parse(init.body as string)).toEqual({ raw: '{"a":1}' });
    expect(headerOf(init, "content-type")).toBe("application/json");
  });

  it("keeps a caller-supplied content-type instead of clobbering it", async () => {
    const { client, backendCalls } = harness();
    await client.authFetch("/v1/thing", {
      method: "PATCH",
      body: { name: "renamed" },
      headers: { "Content-Type": "application/merge-patch+json" },
    });

    const init = backendCalls()[0].init;
    expect(headerOf(init, "content-type")).toBe("application/merge-patch+json");
    expect(JSON.parse(init.body as string)).toEqual({ name: "renamed" });
  });

  it("GET never carries a body (even if one is supplied)", async () => {
    const { client, backendCalls } = harness();
    await client.authFetch("/v1/thing", { method: "GET", body: { ignored: true } });
    expect(backendCalls()[0].init.body).toBeUndefined();
  });

  it("re-sends the identical FormData body across the 401 → refresh → retry", async () => {
    let status: 401 | 200 = 401;
    const { client, backendCalls, refreshCalls } = harness(() => {
      const current = status;
      status = 200;
      return current === 401 ? new Response(null, { status: 401 }) : jsonResponse({ ok: true });
    });
    const form = new FormData();
    form.append("workspace_id", "ws-1");

    const result = await client.authFetch("/v1/documents", { method: "POST", body: form });

    expect(result).toEqual({ ok: true });
    expect(refreshCalls()).toBe(1);
    const attempts = backendCalls();
    expect(attempts).toHaveLength(2);
    expect(attempts[1].init.body).toBe(form); // same instance, still untouched
    expect(headerOf(attempts[1].init, "content-type")).toBeNull();
  });

  it("login/register/logout go through the same boundary (no second decision)", async () => {
    const { fetcher, calls } = scriptedFetcher([
      { match: "/api/auth/login", respond: () => jsonResponse({ ok: true }) },
    ]);
    const client = createAuthClient({ fetcher, navigate: () => {} });
    await client.login("user@example.com", "pw");

    const call = calls.find((entry) => entry.url.includes("/api/auth/login"))!;
    expect(typeof call.init.body).toBe("string");
    const decoded: unknown = JSON.parse(call.init.body as string);
    expect(decoded).toEqual({ email: "user@example.com", password: "pw" });
    expect(decoded).not.toBeTypeOf("string"); // encoded once, through serializeBody
    expect(headerOf(call.init, "content-type")).toBe("application/json");
  });

  it("preserves error handling on a body-carrying request (AuthApiError)", async () => {
    const { client } = harness(() =>
      jsonResponse({ detail: { error: "validation_error", message: "Bad input." } }, 422),
    );
    await expect(
      drive(client.authFetch("/v1/missing", { method: "POST", body: { a: 1 } })),
    ).rejects.toMatchObject({ name: "AuthApiError", status: 422, code: "validation_error" });
  });
});

describe("sanitizeNextPath", () => {
  it.each([
    ["/workspaces", "/workspaces"],
    ["/login?next=/x", null], // nesting next is senseless; same-origin rule still applies
    ["//evil.example", null],
    ["http://evil.example", null],
    ["/\\evil", null],
    ["", null],
    [undefined, null],
  ] as Array<[string | undefined, string | null]>)("next=%j → %j", (input, expected) => {
    expect(sanitizeNextPath(input)).toBe(expected);
  });
});

describe("vi sanity", () => {
  it("mock engine is alive", () => {
    expect(typeof vi.fn()).toBe("function");
  });
});

beforeEach(() => {
  // Isolation is provided per test by fresh clients; nothing global to reset.
});
