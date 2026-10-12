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
 * JSON included — byte-for-byte, with a hard size bound and defined
 * cancellation (see `boundedBodyStream`).
 */

const APP_PROXY_TIMEOUT_MS = 15_000;
/**
 * Hard cap on forwarded request-body bytes. Mirrors the backend document cap
 * (`packages/ingestion/normalize.py` → `MAX_DOCUMENT_BYTES = 25 MiB`), so the
 * proxy never buffers/streams more than the API would accept anyway.
 */
export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
const ALLOWED_METHODS = ["GET", "POST", "PATCH", "PUT", "DELETE"] as const;

/**
 * Raised by the bounded body stream when the ACTUAL byte count (not the
 * declared Content-Length) exceeds `MAX_UPLOAD_BYTES`. `instanceof` checks on
 * this class are how `forward()` maps a mid-stream overflow to a clean 413.
 */
export class UploadTooLargeError extends Error {
  readonly limitBytes: number;

  constructor(limitBytes: number) {
    super(`Request body exceeds the ${limitBytes}-byte proxy upload limit.`);
    this.name = "UploadTooLargeError";
    this.limitBytes = limitBytes;
  }
}

/**
 * Pass raw chunks through UNCHANGED while counting them (T-V1-012 bound).
 *
 * - No decode, no re-encode: chunks are enqueued as received, so multipart
 *   boundaries / invalid UTF-8 / JSON bytes all survive verbatim.
 * - Overflow ERRORS the stream instead of buffering past the limit; the
 *   in-flight upstream upload is thereby aborted.
 * - Cancellation is defined both ways: cancelling the returned stream
 *   cancels `source` (pipeThrough propagates), so a client disconnect tears
 *   down the inbound body; an upstream failure cancels the bounded stream,
 *   which tears down the inbound body the same way.
 */
export function boundedBodyStream(
  source: ReadableStream<Uint8Array>,
  limitBytes: number,
): ReadableStream<Uint8Array> {
  let seen = 0;
  return source.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        seen += chunk.byteLength;
        if (seen > limitBytes) {
          controller.error(new UploadTooLargeError(limitBytes));
          return;
        }
        controller.enqueue(chunk);
      },
    }),
  );
}

/** Clean, backend-shaped reject for oversized uploads (413). */
function uploadTooLargeResponse(): Response {
  return Response.json(
    {
      ok: false,
      error: {
        code: "upload_too_large",
        message: `Upload exceeds the ${MAX_UPLOAD_BYTES}-byte limit accepted by the proxy.`,
      },
    },
    { status: 413 },
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
    // Advisory Content-Length fence (mirrors the backend upload fence): a
    // DECLARED oversize is rejected before a single byte is read and before
    // the backend is contacted. Non-numeric values are ignored (as the
    // backend does); the bounded stream below still enforces the real count.
    const declared = request.headers.get("content-length");
    if (declared !== null && /^\d+$/.test(declared) && Number(declared) > MAX_UPLOAD_BYTES) {
      return uploadTooLargeResponse();
    }
    // Raw/stream forwarding: hand upstream the inbound byte stream itself,
    // bounded by the ACTUAL byte count. No text()/arrayBuffer() decode → no
    // re-encode → multipart, invalid-UTF-8 binary and JSON all reach the
    // backend byte-for-byte (T-V1-010's transport contract: never
    // re-serialize JSON bodies either — the bytes auth-client produced flow
    // through untouched).
    if (request.body) body = boundedBodyStream(request.body, MAX_UPLOAD_BYTES);
  }

  let upstream: Response;
  try {
    // Cancellation contract (T-V1-012): a client hang-up (request.signal)
    // cancels the upstream request and, via the bounded stream's cancel
    // propagation, the inbound body; the fixed timeout still bounds total
    // upstream time.
    const signal = AbortSignal.any([
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
      signal,
    };
    upstream = await fetch(url, init);
  } catch (error: unknown) {
    // Upstream call failed or was cancelled: tear the inbound body stream
    // down too (cancel propagates to the original request body), so no
    // half-consumed upload is left hanging. No-op (rejected, swallowed) when
    // the stream was already fully read or cancelled by fetch itself.
    if (body) void body.cancel().catch(() => undefined);

    if (error instanceof UploadTooLargeError) {
      // Mid-stream overflow (declared length absent or lying): the bounded
      // stream errored, aborting the upstream upload — nothing was buffered
      // past the limit.
      return uploadTooLargeResponse();
    }
    if (request.signal.aborted) {
      // The CLIENT hung up first: the upstream request has been cancelled and
      // there is nobody left to answer. 499 (nginx convention, valid Response
      // status) keeps logs honest about who ended the request.
      return Response.json(
        {
          ok: false,
          error: { code: "client_disconnected", message: "Client closed the request." },
        },
        { status: 499 },
      );
    }
    return Response.json(
      {
        ok: false,
        error: {
          code: "backend_unreachable",
          message:
            error instanceof Error && error.name === "TimeoutError"
              ? `Backend at ${base} timed out.`
              : `Backend at ${base} is unreachable.`,
        },
      },
      { status: 502 },
    );
  }

  const text = await upstream.text();

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
