import { cookies } from "next/headers";
import { getBackendBaseUrl } from "@/lib/backend";
import { ACCESS_COOKIE } from "@/app/(auth)/api/auth/_lib/cookies";

/**
 * The ONE API passage-fetch route (T-V1-032 — RP-13/QF-13).
 *
 * `GET /api/passage?workspace_id=…&chunk_id=…` returns the AUTHORITATIVE
 * passage for exactly one citation:
 *
 *  - the caller's `kios_access` cookie is exchanged server-side for
 *    `Authorization: Bearer …` and forwarded to the backend, so the ACL check
 *    (R-004: authorization before content) happens on the backend for THIS
 *    caller — the browser never gets to ask for a chunk it cannot read;
 *  - the upstream path is the canonical-source read
 *    `GET /v1/workspaces/{workspace_id}/chunks/{chunk_id}` owned by
 *    T-V1-022 (bulk canonical hydration). That endpoint is a dependency and
 *    may not exist yet: upstream 404 is therefore mapped to an EXPLICIT
 *    `passage_unavailable` envelope instead of an opaque failure, so the UI
 *    can render a truthful "not available yet / not found / not authorized"
 *    state rather than borrowing another message's passage.
 *
 * Deliberately a dedicated route rather than a generic proxy pass-through:
 * it validates the two required refs, normalizes the upstream body into one
 * stable `passage` shape for the panel, and owns the honest error taxonomy
 * (`passage_unavailable`, `passage_malformed`, `backend_unreachable`).
 */

/** Ceiling on the whole upstream exchange (headers AND body). */
export const PASSAGE_TIMEOUT_MS = 10_000;

/** Backend canonical-source read path (T-V1-022 contract). */
export const PASSAGE_UPSTREAM_SEGMENTS = "v1/workspaces" as const;

export type PassageParams =
  | { ok: true; workspaceId: string; chunkId: string }
  | { ok: false; message: string };

/**
 * Validate the two required refs. Deliberately length/charset checks only —
 * the backend is the authority on uuid/chunk-id validity (and answers 404/422
 * honestly), while this keeps path injection (`/`, `..`) impossible here.
 */
export function readPassageParams(searchParams: URLSearchParams): PassageParams {
  const workspaceId = searchParams.get("workspace_id")?.trim() ?? "";
  const chunkId = searchParams.get("chunk_id")?.trim() ?? "";
  if (!workspaceId || !chunkId) {
    return { ok: false, message: "Both workspace_id and chunk_id are required." };
  }
  // Refs are single path segments by construction: no separators, no
  // whitespace/control characters, no leading dot (path traversal). The URL
  // builder percent-encodes on top of this, so this is defence in depth.
  const separator = /[/\\\s\u0000-\u001f\u007f]/;
  const traversal = /^\./;
  if (
    separator.test(workspaceId) ||
    separator.test(chunkId) ||
    traversal.test(workspaceId) ||
    traversal.test(chunkId)
  ) {
    return { ok: false, message: "workspace_id/chunk_id contain invalid characters." };
  }
  return { ok: true, workspaceId, chunkId };
}

/** Upstream canonical-passage URL (segments percent-encoded, never interpolated raw). */
export function buildPassageUpstreamUrl(
  base: string,
  workspaceId: string,
  chunkId: string,
): string {
  return `${base}/${PASSAGE_UPSTREAM_SEGMENTS}/${encodeURIComponent(workspaceId)}/chunks/${encodeURIComponent(chunkId)}`;
}

function errorResponse(status: number, code: string, message: string): Response {
  return Response.json({ ok: false, error: { code, message } }, { status });
}

function readErrorMessage(body: unknown, fallback: string): string {
  if (typeof body !== "object" || body === null) return fallback;
  const detail = (body as Record<string, unknown>).detail;
  if (typeof detail === "string" && detail.length > 0) return detail;
  if (typeof detail === "object" && detail !== null) {
    const record = detail as Record<string, unknown>;
    if (typeof record.message === "string" && record.message.length > 0) return record.message;
  }
  return fallback;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function tuple(value: unknown, length: 2 | 4): [number, number] | [number, number, number, number] | null {
  if (!Array.isArray(value) || value.length !== length) return null;
  const numbers = value.map(finiteNumber);
  return numbers.every((n): n is number => n !== null)
    ? (numbers as [number, number] | [number, number, number, number])
    : null;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

export type NormalizedPassage = {
  chunk_id: string | null;
  content: string;
  document_id: string | null;
  filename: string | null;
  page: number | null;
  section: string | null;
  span: [number, number] | null;
  bbox: [number, number, number, number] | null;
  workspace_id: string | null;
};

/**
 * Map the upstream canonical body onto the panel's single shape. `content`
 * is the canonical passage text; `text` is accepted as an alias. Returns
 * null when no readable passage text is present, so the caller can answer
 * `passage_malformed` instead of handing the UI an empty "success".
 */
export function normalizePassage(body: unknown): NormalizedPassage | null {
  if (typeof body !== "object" || body === null) return null;
  const record = body as Record<string, unknown>;
  const content = nullableString(record.content) ?? nullableString(record.text);
  if (content === null) return null;
  return {
    chunk_id: nullableString(record.chunk_id),
    content,
    document_id: nullableString(record.document_id),
    filename: nullableString(record.filename),
    page: finiteNumber(record.page),
    section: nullableString(record.section),
    span: tuple(record.span, 2) as [number, number] | null,
    bbox: tuple(record.bbox, 4) as [number, number, number, number] | null,
    workspace_id: nullableString(record.workspace_id),
  };
}

/**
 * Passage fetch: caller-authenticated, ACL-checked upstream, honestly mapped.
 * Returns 401 (no session), 400 (bad refs), 404 `passage_unavailable`,
 * 403 `forbidden`, 502 `backend_unreachable`/`passage_malformed`, or
 * 200 `{ok:true, passage}`.
 */
export async function GET(request: Request): Promise<Response> {
  const params = readPassageParams(new URL(request.url).searchParams);
  if (!params.ok) {
    return errorResponse(400, "invalid_request", params.message);
  }

  const jar = await cookies();
  const accessToken = jar.get(ACCESS_COOKIE)?.value ?? null;
  if (accessToken === null || accessToken.length === 0) {
    return errorResponse(401, "unauthorized", "Sign in to read a passage.");
  }

  const base = getBackendBaseUrl();
  const url = buildPassageUpstreamUrl(base, params.workspaceId, params.chunkId);

  let upstream: Response;
  let deadlineFired = false;
  try {
    const deadline = AbortSignal.timeout(PASSAGE_TIMEOUT_MS);
    deadline.addEventListener("abort", () => {
      deadlineFired = true;
    });
    upstream = await fetch(url, {
      method: "GET",
      headers: { accept: "application/json", authorization: `Bearer ${accessToken}` },
      cache: "no-store",
      signal: AbortSignal.any([deadline, request.signal]),
    });
  } catch (error: unknown) {
    if (request.signal.aborted) {
      return errorResponse(499, "client_disconnected", "Client closed the request.");
    }
    const timedOut =
      deadlineFired || (error instanceof Error && error.name === "TimeoutError");
    return errorResponse(
      502,
      "backend_unreachable",
      timedOut
        ? `Backend at ${base} timed out fetching the canonical passage.`
        : `Backend at ${base} is unreachable.`,
    );
  }

  const status = upstream.status;

  if (status === 404) {
    // Explicit: either the canonical-source read is not deployed yet
    // (T-V1-022 pending) or the chunk is missing/deleted/out of scope.
    return errorResponse(
      404,
      "passage_unavailable",
      "No canonical passage for this citation: the backend canonical-source read is not available yet (T-V1-022) or this chunk no longer exists in your workspace.",
    );
  }
  if (status === 401) {
    return errorResponse(401, "unauthorized", "Session expired. Sign in again to read passages.");
  }
  if (status === 403) {
    return errorResponse(
      403,
      "forbidden",
      "You are not authorized to read this passage (workspace scope denied).",
    );
  }
  if (status >= 500) {
    return errorResponse(502, "backend_unreachable", `Backend at ${base} returned ${status}.`);
  }

  let body: unknown = null;
  try {
    const text = await upstream.text();
    body = text.length > 0 ? JSON.parse(text) : null;
  } catch {
    body = null;
  }

  if (!upstream.ok) {
    // Remaining upstream 4xx (400/422/…): surface the backend's own reason.
    return errorResponse(
      status === 400 || status === 422 ? 400 : status,
      status === 400 || status === 422 ? "invalid_request" : "passage_rejected",
      readErrorMessage(body, `Passage request rejected by the backend (HTTP ${status}).`),
    );
  }

  const passage = normalizePassage(body);
  if (passage === null) {
    return errorResponse(
      502,
      "passage_malformed",
      "Backend answered without readable canonical passage text.",
    );
  }

  return Response.json(
    { ok: true, passage },
    { status: 200, headers: { "cache-control": "no-store" } },
  );
}
