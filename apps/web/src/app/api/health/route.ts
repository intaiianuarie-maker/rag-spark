import { NextResponse } from "next/server";
import { BACKEND_TIMEOUT_MS, getBackendBaseUrl } from "@/lib/backend";
import {
  normalizeHealthPayload,
  type HealthApiResponse,
  type HealthApiError,
} from "@/lib/health";

/**
 * Same-origin proxy for the backend health endpoint (architecture.md contract
 * `GET /health`). The browser never talks to the backend directly; calls to the
 * API go through this route, so the status page works without CORS and shows
 * an explicit error state whenever the backend is down.
 *
 * No `use cache`: the handler must execute on every request (health status is
 * live data). `cache: "no-store"` below keeps the backend fetch uncached.
 */

export async function GET(): Promise<Response> {
  const url = `${getBackendBaseUrl()}/health`;

  let backend: Response;
  try {
    backend = await fetch(url, {
      cache: "no-store",
      headers: { accept: "application/json" },
      signal: AbortSignal.timeout(BACKEND_TIMEOUT_MS),
    });
  } catch (error: unknown) {
    return errorResponse("backend_unreachable", describeNetworkError(error), 502);
  }

  // Try to interpret the body as JSON regardless of status; the backend may
  // still report useful fields (e.g. a 503 payload during startup).
  let body: unknown = null;
  let isJson = false;
  try {
    const text = await backend.text();
    if (text.trim().length > 0) {
      body = JSON.parse(text);
      isJson = true;
    }
  } catch {
    isJson = false;
  }

  if (!backend.ok) {
    const health = isJson ? normalizeHealthPayload(body) : undefined;
    return NextResponse.json(
      {
        ok: false,
        error: "backend_unhealthy",
        message: `Backend ${url} responded with HTTP ${backend.status}.`,
        status: backend.status,
        ...(health && !health.isEmpty ? { health } : {}),
      } satisfies HealthApiResponse,
      { status: backend.status },
    );
  }

  if (!isJson) {
    return errorResponse(
      "unexpected_response",
      `Backend ${url} returned HTTP ${backend.status} with a non-JSON body.`,
      502,
    );
  }

  return NextResponse.json(
    {
      ok: true,
      fetchedAt: new Date().toISOString(),
      health: normalizeHealthPayload(body),
    } satisfies HealthApiResponse,
    { status: 200 },
  );
}

function errorResponse(
  error: HealthApiError,
  message: string,
  status: number,
): Response {
  return NextResponse.json(
    { ok: false, error, message } satisfies HealthApiResponse,
    { status },
  );
}

function describeNetworkError(error: unknown): string {
  const at = `Could not reach backend /health (${BACKEND_TIMEOUT_MS / 1000}s timeout, base URL: ${getBackendBaseUrl()}).`;
  if (error instanceof Error && error.name === "TimeoutError") {
    return `${at} The request timed out.`;
  }
  if (error instanceof Error && error.cause) {
    const cause = error.cause as { message?: string; errors?: unknown[] };
    const causeText =
      typeof cause.message === "string" && cause.message.length > 0
        ? cause.message
        : Array.isArray(cause.errors) && cause.errors.length > 0
          ? cause.errors.map((entry) => String(entry)).join("; ")
          : String(cause);
    return `${at} Cause: ${causeText}.`;
  }
  return at;
}
