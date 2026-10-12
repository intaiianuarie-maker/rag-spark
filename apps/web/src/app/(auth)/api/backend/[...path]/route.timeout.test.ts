/**
 * T-V1-041 — proxy timeout must cover slow Asks.
 *
 * Trace: T-V1-011 measured 8.6–16.7 s for a GOOD Ask answer through the live
 * gateway while `APP_PROXY_TIMEOUT_MS` was 15 s — the proxy converted those
 * upstream 200s into "backend_unreachable" 502s. These tests pin the fix:
 *
 *  1. the documented cap covers the measured ~17 s worst case AND stays a
 *     hard bound (no unbounded wait);
 *  2. a 16 s upstream 200 is forwarded as 200 — never converted to 502
 *     (under the old 15 s cap this test fails: the deadline fires at 15 s
 *     and `forward()` answers 502);
 *  3. fast paths are unchanged (composed signal attached, not fired; auth +
 *     content-type forwarded verbatim);
 *  4. cancellation still propagates through the composed signal (client
 *     hang-up → 499, upstream request aborted);
 *  5. a body that stalls AFTER the headers arrive is mapped to the same
 *     error contract instead of escaping `forward()` as an unhandled
 *     rejection (opaque 500).
 *
 * Same harness as `route.test.ts`: only global `fetch` + `next/headers` are
 * stubbed; the real `forward()` / route `POST()` handler drive everything.
 * Test 2 waits the full 16 s on purpose — that delay IS the evidence; its
 * per-test timeout is raised accordingly.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const cookieGetMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: cookieGetMock })),
}));

import { forward, POST, APP_PROXY_TIMEOUT_MS } from "./route";

const backendFetch = vi.fn<(input: RequestInfo, init?: RequestInit) => Promise<Response>>();

/** Worst measured Ask latency through the gateway (T-V1-011): 16.7 s. */
const MEASURED_WORST_CASE_MS = 16_700;
/** The stub upstream's delay — the ticket's "16 s backend" case. */
const SLOW_UPSTREAM_MS = 16_000;

/** An Ask-shaped request (POST + JSON body), optionally with a client signal. */
function askRequest(signal?: AbortSignal): Request {
  return new Request("http://web.local/api/backend/v1/ask", {
    method: "POST",
    body: '{"query":"retrieval precision RP-06?","model":"gpt-4o-mini"}',
    headers: { "content-type": "application/json" },
    signal,
  });
}

const ASK_CONTEXT = { params: Promise.resolve({ path: ["v1", "ask"] }) };

/**
 * Stub that behaves like the network: resolves `delayMs` later, or rejects
 * the moment the proxy's composed signal fires — with the error
 * `AbortSignal.timeout` produces (`DOMException` named "TimeoutError"), so a
 * premature deadline surfaces exactly as it would under real undici.
 */
function slowBackend(delayMs: number, response: () => Response) {
  return (_input: RequestInfo, init?: RequestInit): Promise<Response> =>
    new Promise<Response>((resolve, reject) => {
      const timer = setTimeout(() => resolve(response()), delayMs);
      init?.signal?.addEventListener("abort", () => {
        clearTimeout(timer);
        reject(new DOMException("The operation timed out.", "TimeoutError"));
      });
    });
}

function slowAskAnswer(): Response {
  return new Response(JSON.stringify({ answer: "slow but grounded", status: "answered" }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

describe("T-V1-041 proxy timeout covers slow Asks", () => {
  beforeEach(() => {
    backendFetch.mockReset();
    vi.stubGlobal("fetch", backendFetch as unknown as typeof fetch);
    cookieGetMock.mockReturnValue({ value: "test-access-token" });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps a documented cap that covers the measured ~17s worst case and stays bounded", () => {
    // No unbounded wait: the cap is a finite, hard deadline…
    expect(Number.isFinite(APP_PROXY_TIMEOUT_MS)).toBe(true);
    // …that covers the worst GOOD answer T-V1-011 measured (16.7 s), with
    // margin — a 16 s upstream must never hit it.
    expect(APP_PROXY_TIMEOUT_MS).toBeGreaterThan(MEASURED_WORST_CASE_MS);
    // …and is still a sane ceiling, not an open-ended wait.
    expect(APP_PROXY_TIMEOUT_MS).toBeLessThanOrEqual(60_000);
  });

  it(
    "forwards a 16s-upstream 200 as 200 — never converted to 502",
    async () => {
      backendFetch.mockImplementationOnce(slowBackend(SLOW_UPSTREAM_MS, slowAskAnswer));

      const startedAt = Date.now();
      const response = await POST(askRequest(), ASK_CONTEXT);
      const elapsedMs = Date.now() - startedAt;

      // THE regression guard: under the old 15 s cap the stub's abort fires
      // first and forward() answers 502 backend_unreachable.
      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toEqual({
        answer: "slow but grounded",
        status: "answered",
      });
      // The stub really waited out the slow upstream (this is not a
      // shortcut path) and the deadline never aborted it early.
      expect(elapsedMs).toBeGreaterThanOrEqual(SLOW_UPSTREAM_MS);
      const [, init] = backendFetch.mock.calls[0];
      expect(init?.signal?.aborted).toBe(false);
    },
    SLOW_UPSTREAM_MS + 10_000, // the 16 s wait is the evidence; allow it
  );

  it("fast paths unchanged: composed signal attached but unfired, headers verbatim", async () => {
    backendFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ results: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const response = await POST(askRequest(), ASK_CONTEXT);

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ results: [] });
    const [, init] = backendFetch.mock.calls[0];
    // The deadline+cancel signal is still composed and passed upstream…
    expect(init?.signal).toBeInstanceOf(AbortSignal);
    // …but it has not fired on the fast path (a ceiling, not a delay).
    expect(init?.signal?.aborted).toBe(false);
    const headers = init?.headers as Record<string, string>;
    expect(headers.authorization).toBe("Bearer test-access-token");
    expect(headers["content-type"]).toBe("application/json");
  });

  it("cancel propagates: client hang-up aborts the upstream request (499)", async () => {
    const abortController = new AbortController();
    let upstreamSawAbort = false;
    backendFetch.mockImplementationOnce(
      (_input, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            upstreamSawAbort = true;
            reject(new DOMException("This operation was aborted", "AbortError"));
          });
          // never resolves — only the abort can end this exchange
        }),
    );

    const pending = forward(askRequest(abortController.signal), ["v1", "ask"]);
    await new Promise((resolve) => setTimeout(resolve, 20)); // let the send start
    abortController.abort(); // client hangs up
    const response = await pending;

    expect(response.status).toBe(499);
    const payload = (await response.json()) as { ok: boolean; error: { code: string } };
    expect(payload.ok).toBe(false);
    expect(payload.error.code).toBe("client_disconnected");
    expect(upstreamSawAbort).toBe(true); // cancel reached the upstream request
  });

  it("maps a post-headers body stall to the error contract (no unhandled 500)", async () => {
    // Headers arrive immediately; the BODY never completes and is torn down
    // when the composed signal aborts (mirrors undici). Client hang-up
    // mid-body must stay the documented 499 instead of escaping forward()
    // as an unhandled rejection → opaque 500. The same deadline also bounds
    // this stall in the no-client-abort case (see APP_PROXY_TIMEOUT_MS).
    const abortController = new AbortController();
    backendFetch.mockImplementationOnce((_input, init) =>
      Promise.resolve(
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              init?.signal?.addEventListener("abort", () => {
                controller.error(new DOMException("This operation was aborted", "AbortError"));
              });
              // no enqueue, no close — the body stalls until aborted
            },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    const pending = forward(askRequest(abortController.signal), ["v1", "ask"]);
    await new Promise((resolve) => setTimeout(resolve, 20)); // headers received
    abortController.abort(); // client hangs up while the body stalls
    const response = await pending;

    expect(response.status).toBe(499);
    const payload = (await response.json()) as { ok: boolean; error: { code: string } };
    expect(payload.ok).toBe(false);
    expect(payload.error.code).toBe("client_disconnected");
  });
});
