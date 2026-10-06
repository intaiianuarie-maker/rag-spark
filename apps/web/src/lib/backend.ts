/**
 * Backend base URL for server-side calls (used by route handlers only).
 *
 * The status page always talks to this web app's own /api/health route, which
 * proxies to the FastAPI backend. This keeps the browser same-origin and avoids
 * depending on CORS configuration in the API (M0: adapter-friendly, no coupling).
 *
 * Configuration (see .env.example):
 * - API_BASE_URL: backend origin as seen from the web container.
 *   Default: http://localhost:8000 (local dev outside Docker).
 *   In docker compose this must be http://api:8000.
 */
const DEFAULT_API_BASE_URL = "http://localhost:8000";

export function getBackendBaseUrl(): string {
  const raw = process.env.API_BASE_URL?.trim();
  const base = raw && raw.length > 0 ? raw : DEFAULT_API_BASE_URL;
  // Normalize: drop trailing slashes so `${base}/health` is always well-formed.
  return base.replace(/\/+$/, "");
}

/**
 * Timeout for backend health requests (ms). Deliberately short: the status
 * page must render an explicit error state quickly when the backend is down.
 */
export const BACKEND_TIMEOUT_MS = 3_000;
