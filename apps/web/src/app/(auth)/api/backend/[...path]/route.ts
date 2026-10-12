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
 */

const APP_PROXY_TIMEOUT_MS = 15_000;
const ALLOWED_METHODS = ["GET", "POST", "PATCH", "PUT", "DELETE"] as const;

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
    // Body-carrying methods: forward the declared content-type when present.
    const contentType = request.headers.get("content-type");
    if (contentType) headers["content-type"] = contentType;
  }

  const hasBody = method !== "GET" && method !== "DELETE";
  let body: BodyInit | undefined = undefined;
  if (hasBody) {
    const ct = request.headers.get("content-type") || "";
    if (ct.includes("multipart/") || ct.includes("application/octet-stream")) {
      const buffer = await request.arrayBuffer();
      body = Buffer.from(buffer);
    } else {
      body = await request.text();
    }
  }

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method,
      headers,
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(APP_PROXY_TIMEOUT_MS),
    });
  } catch (error: unknown) {
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
