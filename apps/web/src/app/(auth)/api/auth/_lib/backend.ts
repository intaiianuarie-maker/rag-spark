/**
 * Backend auth calls for the (auth) proxy handlers.
 *
 * Binding contract (M1-001a, frozen by test report): FastAPI returns tokens in
 * the JSON body and NEVER sets cookies; this proxy owns all Set-Cookie.
 * Backend error bodies are nested: {"detail": {"error", "message"}} (422 adds
 * a redacted `detail` issues array). We normalize once, here, into the proxy
 * error shape the browser client understands.
 */

import { BACKEND_TIMEOUT_MS, getBackendBaseUrl } from "@/lib/backend";

export type AuthBackendPath =
  | "/auth/register"
  | "/auth/login"
  | "/auth/logout"
  | "/auth/refresh";

export type BackendOutcome =
  | {
      kind: "response";
      status: number;
      body: unknown;
      retryAfter?: number;
    }
  | { kind: "unreachable"; detail: string };

export function postToAuthBackend(
  path: AuthBackendPath,
  body: unknown,
): Promise<BackendOutcome> {
  return postToBackend(path, body, BACKEND_TIMEOUT_MS);
}

async function postToBackend(
  path: string,
  body: unknown,
  timeoutMs: number,
): Promise<BackendOutcome> {
  let response: Response;
  try {
    response = await fetch(`${getBackendBaseUrl()}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(body ?? {}),
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (error: unknown) {
    return { kind: "unreachable", detail: describeNetworkError(error) };
  }

  const retryAfterHeader = response.headers.get("retry-after");
  const retryAfterSeconds =
    retryAfterHeader && Number.isFinite(Number(retryAfterHeader))
      ? Math.max(0, Number(retryAfterHeader))
      : undefined;

  let parsed: unknown = null;
  try {
    const text = await response.text();
    if (text.trim().length > 0) parsed = JSON.parse(text);
  } catch {
    parsed = null;
  }
  return { kind: "response", status: response.status, body: parsed, retryAfter: retryAfterSeconds };
}

export type ExtractedError = {
  code: string;
  message: string;
  fields?: Record<string, string[]>;
};

/**
 * Normalizes backend error payloads into one proxy shape. Accepts the frozen
 * nested shape `{"detail": {"error", "message"}}`, the validation shape
 * `{"detail": [issues], "error": "validation_error", "message": ...}`, and
 * (defensively) the flat `{"error", "message"}` shape.
 */
export function extractBackendError(body: unknown): ExtractedError {
  if (typeof body !== "object" || body === null) {
    return { code: "request_failed", message: "Authentication request failed." };
  }
  const record = body as Record<string, unknown>;

  const detail = record.detail;
  if (Array.isArray(detail)) {
    const fields: Record<string, string[]> = {};
    for (const issue of detail) {
      if (typeof issue !== "object" || issue === null) continue;
      const item = issue as { loc?: unknown; msg?: unknown };
      const loc = Array.isArray(item.loc) ? item.loc : [];
      const field = loc.length > 0 ? String(loc[loc.length - 1]) : "form";
      const message = typeof item.msg === "string" ? item.msg : "Invalid value.";
      (fields[field] ??= []).push(message);
    }
    return {
      code: typeof record.error === "string" ? record.error : "validation_error",
      message: typeof record.message === "string" ? record.message : "Please review the highlighted fields.",
      fields,
    };
  }

  if (typeof detail === "object" && detail !== null) {
    const nested = detail as Record<string, unknown>;
    return {
      code: typeof nested.error === "string" ? nested.error : "request_failed",
      message: typeof nested.message === "string" ? nested.message : "Authentication request failed.",
    };
  }

  return {
    code: typeof record.error === "string" ? record.error : "request_failed",
    message: typeof record.message === "string" ? record.message : "Authentication request failed.",
  };
}

export function describeNetworkError(error: unknown): string {
  const at = `Backend ${getBackendBaseUrl()} unreachable (${BACKEND_TIMEOUT_MS / 1000}s timeout).`;
  if (error instanceof Error && error.name === "TimeoutError") {
    return `${at} The request timed out.`;
  }
  if (error instanceof Error && error.cause) {
    const cause = error.cause as { message?: string };
    return `${at} Cause: ${cause.message ?? String(error.cause)}.`;
  }
  return at;
}
