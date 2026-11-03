import { cookies } from "next/headers";
import { getBackendBaseUrl } from "@/lib/backend";
import { ACCESS_COOKIE } from "@/app/(auth)/api/auth/_lib/cookies";

/**
 * Generic same-origin app-API proxy (M1-002b/c and M2 pages will call this
 * with `authFetch`). Responsibilities:
 *
 *  - attach `Authorization: Bearer <kios_access>` from the httpOnly cookie
 *    (JS cannot read the cookie, so the browser can never supply the header);
 *  - forward method/path/query/body verbatim to the FastAPI backend;
 *  - return upstream status/body unchanged.
 *
 * Deliberately DOES NOT refresh: 401-on-access is the client's signal to run
 * the single-flight refresh flow and retry exactly once (boss ruling:
 * "401-on-access = refresh flow"). Keeping the proxy dumb avoids duplicate
 * rotation logic across two layers.
 *
 * Upload forwarding (T-V1-012, RP-07/QF-07): request bodies cross this proxy
 * as RAW BYTES on a stream. There is deliberately no `request.text()` /
 * `json()` anywhere on the request path — a UTF-8 decode→re-encode round-trip
 * mutates invalid-UTF-8 binary (the 134→140-byte upload corruption this
 * ticket closes) and is a second serialization decision. This is the
 * transport half of the T-V1-010 contract: auth-client owns the single
 * object→string serialization; the proxy moves whatever bytes arrive —
 * JSON included — byte-for-byte, with defined cancellation (a client hang-up
 * tears down the inbound body; see `forward`).
 */

/**
 * Hard cap on how long the proxy waits on the upstream backend (T-V1-041).
 *
 * The deadline covers the WHOLE upstream exchange — response headers AND
 * body — because the composed signal stays attached to the body stream after
 * `fetch` resolves. It is a fixed bound, never an unbounded wait: at this
 * deadline the upstream request is aborted no matter what the backend does,
 * and a client hang-up (`request.signal`, composed in below) can only end it
 * EARLIER.
 *
 * Raised 15 s → 30 s: T-V1-011 measured 8.6–16.7 s for a GOOD Ask answer
 * against the live gateway; at 15 s the proxy converted exactly those
 * upstream 200s into "backend_unreachable" 502s. 30 s covers the measured
 * ~17 s worst case with ~2x margin. Fast paths are unaffected (the deadline
 * is a ceiling, not a delay) and the raise costs nothing on cancellation.
 */
export const APP_PROXY_TIMEOUT_MS = 30_000;
const ALLOWED_METHODS = ["GET", "POST", "PATCH", "PUT", "DELETE"] as const;

/**
 * Map a failed upstream exchange onto the proxy's error contract (shared by
 * the pre-headers failure — `fetch` rejected — and the post-headers body
 * failure, so both surfaces report the same statuses):
 *
 *  - client hung up first (`request.signal.aborted`) → 499 `client_disconnected`
 *    (nobody is left to answer; nginx convention);
 *  - the deadline fired (composed signal aborted, or `fetch` rejected with
 *    its `TimeoutError`) → 502 `backend_unreachable`, timeout wording;
 *  - anything else (connection refused/reset, upstream body error) → 502
 *    `backend_unreachable`, unreachable wording.
 */
function upstreamFailureResponse(
  error: unknown,
  base: string,
  request: Request,
  deadlineFired: boolean,
): Response {
  if (request.signal.aborted) {
    return Response.json(
      {
        ok: false,
        error: { code: "client_disconnected", message: "Client closed the request." },
      },
      { status: 499 },
    );
  }
  const timedOut = deadlineFired || (error instanceof Error && error.name === "TimeoutError");
  return Response.json(
    {
      ok: false,
      error: {
        code: "backend_unreachable",
        message: timedOut
          ? `Backend at ${base} timed out.`
          : `Backend at ${base} is unreachable.`,
      },
    },
    { status: 502 },
  );
}

export async function forward(
  request: Request,
  pathSegments: readonly string[],
): Promise<Response> {
  const method = request.method.toUpperCase() as (typeof ALLOWED_METHODS)[number];
  if (!ALLOWED_METHODS.includes(method)) {
    return Response.json(
      { ok: false, error: { code: "method_not_allowed", message: "Method not supported." } },
      { status: 405 },
    );
  }

  const jar = await cookies();
  const accessToken = jar.get(ACCESS_COOKIE)?.value ?? null;

  const targetPath = pathSegments.map(encodeURIComponent).join("/");
  const query = new URL(request.url).search;
  const base = getBackendBaseUrl();
  const url = `${base}/${targetPath}${query}`;

  const headers: Record<string, string> = { accept: "application/json" };
  if (accessToken !== null) {
    headers.authorization = `Bearer ${accessToken}`;
  }
  if (method !== "GET" && method !== "DELETE") {
    // Body-carrying methods: forward the declared content-type VERBATIM — for
    // multipart uploads that includes the `boundary=…` parameter, which the
    // backend needs to split the parts. Dropping or re-deriving it corrupts
    // every upload, so it is copied as-is (T-V1-012).
    const contentType = request.headers.get("content-type");
    if (contentType) headers["content-type"] = contentType;
  }

  const hasBody = method !== "GET" && method !== "DELETE";
  let body: ReadableStream<Uint8Array> | undefined = undefined;
  if (hasBody) {
    // Raw/stream forwarding: hand upstream the inbound byte stream itself.
    // No text()/arrayBuffer() decode → no re-encode → multipart,
    // invalid-UTF-8 binary and JSON all reach the backend byte-for-byte
    // (T-V1-010's transport contract: never re-serialize JSON bodies either —
    // the bytes auth-client produced flow through untouched).
    if (request.body) body = request.body;
  }

  let upstreamSignal: AbortSignal | null = null;
  let upstream: Response;
  try {
    // Cancellation contract (T-V1-012): a client hang-up (request.signal)
    // cancels the upstream request and the inbound body; the composed deadline
    // still bounds the TOTAL upstream time — headers AND body (see
    // APP_PROXY_TIMEOUT_MS).
    upstreamSignal = AbortSignal.any([
      AbortSignal.timeout(APP_PROXY_TIMEOUT_MS),
      request.signal,
    ]);
    const init: RequestInit & { duplex: "half" } = {
      method,
      headers,
      body,
      // Stream bodies require `duplex: "half"` (fetch spec); the DOM lib's
      // RequestInit type predates the field, hence the intersection type.
      duplex: "half",
      cache: "no-store",
      signal: upstreamSignal,
    };
    upstream = await fetch(url, init);
  } catch (error: unknown) {
    // Upstream call failed or was cancelled: tear the inbound body stream
    // down too (cancel propagates to the original request body), so no
    // half-consumed upload is left hanging. No-op (rejected, swallowed) when
    // the stream was already fully read or cancelled by fetch itself.
    if (body) void body.cancel().catch(() => undefined);

    return upstreamFailureResponse(error, base, request, false);
  }

  // M5-001a REV-2 Finding 1: stream SSE responses through instead of
  // buffering `upstream.text()` first. For text/event-stream the client must
  // receive each token as the backend emits it — awaiting the full body
  // before returning meant nothing rendered until upstream EOF. The deadline
  // signal stays attached to the body stream after `fetch` resolves, so a
  // stalled upstream body is still bounded by APP_PROXY_TIMEOUT_MS (the same
  // cap the text() path had).
  if (isEventStream(upstream)) {
    return streamUpstreamResponse(upstream);
  }

  let text: string;
  try {
    // The deadline signal stays attached to the body stream, so headers AND
    // body are bounded by the SAME cap (T-V1-041). A body that stalls or
    // errors after the headers arrived therefore rejects HERE and is mapped
    // to the error contract below — instead of escaping forward() as an
    // unhandled rejection (an opaque 500 from the route handler). A body
    // that simply arrives (every fast path) is untouched.
    text = await upstream.text();
  } catch (error: unknown) {
    if (body) void body.cancel().catch(() => undefined);
    return upstreamFailureResponse(
      error,
      base,
      request,
      upstreamSignal?.aborted === true && !request.signal.aborted,
    );
  }

  // Bodiless statuses must be forwarded with a null body — constructing a
  // Response with `body: ""` and status 204 throws
  // "TypeError: Invalid response status code 204" (test1 M1-002b report R1).
  const responseBody = [204, 205, 304].includes(upstream.status) ? null : text;

  const responseHeaders = new Headers();
  responseHeaders.set("cache-control", "no-store");
  const contentType = upstream.headers.get("content-type");
  if (contentType && responseBody !== null) responseHeaders.set("content-type", contentType);
  return new Response(responseBody, { status: upstream.status, headers: responseHeaders });
}

/** True when the upstream response is an SSE stream the proxy must forward live. */
function isEventStream(response: Response): boolean {
  const contentType = response.headers.get("content-type") ?? "";
  return contentType.toLowerCase().includes("text/event-stream");
}

/**
 * Stream an upstream response body through to the client WITHOUT buffering it
 * in memory first (M5-001a REV-2 Finding 1).
 *
 * `forward()` used to `await upstream.text()` before returning — for an SSE
 * answer that means the client waits for upstream EOF before rendering a single
 * token. Here the upstream body is piped straight through as a
 * `ReadableStream`, so the first chunk reaches the client as soon as the
 * backend emits it.
 *
 * Headers are forwarded verbatim (content-type, cache-control, and the
 * `X-Accel-Buffering: no` hint the Ask SSE endpoint sets so a reverse proxy
 * never re-buffers the stream). The deadline signal stays attached to the body
 * stream after `fetch` resolves, so a stalled upstream body is still bounded by
 * `APP_PROXY_TIMEOUT_MS` — the same cap as the text() path.
 *
 * A null body (204/205/304, or an upstream that sent headers only) is forwarded
 * as a null body: constructing a Response with `body: ""` and status 204/205
 * throws "Invalid response status code 204".
 */
export function streamUpstreamResponse(upstream: Response): Response {
  const body = upstream.body;
  const headers = new Headers();
  headers.set("cache-control", "no-store");
  for (const key of ["content-type", "x-accel-buffering"]) {
    const value = upstream.headers.get(key);
    if (value) headers.set(key, value);
  }
  return new Response(body, { status: upstream.status, headers });
}

export async function GET(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await context.params;
  return forward(request, path);
}

export async function POST(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await context.params;
  return forward(request, path);
}

export async function PATCH(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await context.params;
  return forward(request, path);
}

export async function PUT(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await context.params;
  return forward(request, path);
}

export async function DELETE(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await context.params;
  return forward(request, path);
}
