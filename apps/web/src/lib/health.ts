/**
 * Health contract shared by the API proxy route handler and the status UI.
 *
 * Source of truth: `GET /health` is implemented by the backend (apps/api).
 * The frontend treats the payload defensively: it only *requires* nothing and
 * recognizes the optional fields the task spec mentions (status, version).
 * Unknown fields are preserved in `details` and rendered as-is, so changes on
 * the backend side cannot silently break the status page.
 */

export type HealthStatus = {
  /** Raw `status` field reported by the backend (e.g. "ok"), or null if absent. */
  status: string | null;
  /** Raw `version` field reported by the backend, or null if absent. */
  version: string | null;
  /** Every other field the backend reported. */
  details: Record<string, unknown>;
  /** True when the payload carried no recognizable information at all. */
  isEmpty: boolean;
};

/** Error kinds reported by `GET /api/health` (this app's proxy). */
export type HealthApiError =
  | "backend_unreachable"
  | "backend_unhealthy"
  | "unexpected_response";

/** Response shape produced by `GET /api/health` (this app's proxy). */
export type HealthApiResponse =
  | { ok: true; fetchedAt: string; health: HealthStatus }
  | {
      ok: false;
      error: HealthApiError;
      message: string;
      status?: number;
      health?: HealthStatus;
    };

export function normalizeHealthPayload(payload: unknown): HealthStatus {
  if (typeof payload !== "object" || payload === null) {
    return { status: null, version: null, details: {}, isEmpty: true };
  }
  const record: Record<string, unknown> = { ...payload };
  const status =
    typeof record.status === "string" || typeof record.status === "number"
      ? String(record.status)
      : null;
  const version =
    typeof record.version === "string" || typeof record.version === "number"
      ? String(record.version)
      : null;
  delete record.status;
  delete record.version;
  const isEmpty = status === null && version === null && !hasRenderableValues(record);
  return { status, version, details: record, isEmpty };
}

/** Derives the display state from the backend `status` string. */
export type OverallState = "healthy" | "degraded" | "down" | "unknown";

export function overallState(health: HealthStatus): OverallState {
  const value = health.status?.toLowerCase();
  if (value === "ok" || value === "healthy" || value === "up") return "healthy";
  if (value === "degraded") return "degraded";
  if (value === "error" || value === "down" || value === "unhealthy") return "down";
  return "unknown";
}

/**
 * Runtime guard for the /api/health response (no validation library by
 * design: M0 keeps frontend dependencies minimal; contract tests come with
 * M0-004). Returns null when the payload is not the shape we understand.
 */
export function parseHealthApiResponse(payload: unknown): HealthApiResponse | null {
  if (typeof payload !== "object" || payload === null) return null;
  const record = payload as Record<string, unknown>;
  if (typeof record.ok !== "boolean") return null;
  if (record.ok) {
    if (typeof record.fetchedAt !== "string") return null;
    return {
      ok: true,
      fetchedAt: record.fetchedAt,
      health: normalizeHealthPayload(record.health),
    };
  }
  const knownErrors: readonly HealthApiError[] = [
    "backend_unreachable",
    "backend_unhealthy",
    "unexpected_response",
  ];
  if (typeof record.error !== "string" || !knownErrors.includes(record.error as never)) {
    return null;
  }
  return {
    ok: false,
    error: record.error as HealthApiError,
    message: typeof record.message === "string" ? record.message : "Backend request failed.",
    status: typeof record.status === "number" ? record.status : undefined,
    health: record.health === undefined ? undefined : normalizeHealthPayload(record.health),
  };
}

function hasRenderableValues(details: Record<string, unknown>): boolean {
  return Object.values(details).some((value) => {
    if (typeof value === "string") return value.trim().length > 0;
    if (typeof value === "number") return Number.isFinite(value);
    if (typeof value === "boolean") return true;
    return value !== null && value !== undefined;
  });
}
