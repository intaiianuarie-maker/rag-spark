"use client";

import { useHealthStatus, type HealthUiState } from "@/lib/use-health";
import { overallState, type OverallState } from "@/lib/health";

/**
 * M0 status page panel (TASK-M0-003 requirement 3): calls the backend health
 * contract via the same-origin /api/health proxy and renders every required
 * state explicitly: loading, error, empty, and success (healthy/degraded/down).
 */

const OVERALL_STYLES: Record<OverallState, { dot: string; label: string; header: string }> = {
  healthy: {
    dot: "bg-emerald-500",
    label: "Operational",
    header: "border-emerald-600/30 dark:border-emerald-400/30",
  },
  degraded: {
    dot: "bg-amber-500",
    label: "Degraded",
    header: "border-amber-600/30 dark:border-amber-400/30",
  },
  down: {
    dot: "bg-red-500",
    label: "Down",
    header: "border-red-600/30 dark:border-red-400/30",
  },
  unknown: {
    dot: "bg-neutral-400",
    label: "Unknown",
    header: "border-neutral-600/30 dark:border-neutral-400/30",
  },
};

export function HealthPanel() {
  const { state, refresh, isRefreshing } = useHealthStatus();

  return (
    <section aria-live="polite" aria-busy={isRefreshing} className="w-full max-w-xl">
      <h1 className="text-2xl font-semibold tracking-tight">System status</h1>
      <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
        Health of the backend service, taken from <code className="font-mono">GET /health</code>.
        Refreshed automatically every 15 seconds.
      </p>
      <div className="mt-6">
        <StateBody state={state} onRetry={refresh} isRefreshing={isRefreshing} />
      </div>
      <p className="mt-4 text-center text-xs text-neutral-500 dark:text-neutral-500">
        Browser → <code className="font-mono">/api/health</code> (web) →{" "}
        <code className="font-mono">/health</code> (FastAPI)
      </p>
    </section>
  );
}

function StateBody({
  state,
  onRetry,
  isRefreshing,
}: {
  state: HealthUiState;
  onRetry: () => void;
  isRefreshing: boolean;
}) {
  if (state.phase === "loading") {
    return (
      <div
        role="status"
        aria-label="Checking backend health"
        className="rounded-xl border border-neutral-200 bg-white p-5 dark:border-neutral-800 dark:bg-neutral-900"
      >
        <div className="flex items-center gap-3">
          <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-neutral-400" />
          <span className="animate-pulse text-sm text-neutral-600 dark:text-neutral-400">
            Checking backend health…
          </span>
        </div>
        <div className="mt-4 space-y-2" aria-hidden="true">
          <div className="h-3 w-2/3 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
          <div className="h-3 w-1/3 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
        </div>
      </div>
    );
  }

  if (state.phase === "error") {
    return (
      <div
        role="alert"
        className="rounded-xl border border-red-600/30 bg-red-50 p-5 dark:border-red-400/30 dark:bg-red-950/40"
      >
        <div className="flex items-center gap-3">
          <span className="h-2.5 w-2.5 rounded-full bg-red-500" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-red-900 dark:text-red-200">
            {state.status === undefined ? "Backend unreachable" : `Backend unhealthy (HTTP ${state.status})`}
          </h2>
        </div>
        <p className="mt-2 break-words font-mono text-xs leading-relaxed text-red-800/90 dark:text-red-200/80">
          {state.message}
        </p>
        <div className="mt-4 flex items-center gap-3">
          <RetryButton onClick={onRetry} isRefreshing={isRefreshing} />
          <p className="text-xs text-neutral-500 dark:text-neutral-400">
            The API may still be starting. Retrying automatically.
          </p>
        </div>
      </div>
    );
  }

  const style = OVERALL_STYLES[overallState(state.health)];

  if (state.health.isEmpty) {
    return (
      <div
        role="status"
        className="rounded-xl border border-dashed border-neutral-300 bg-white p-5 text-center dark:border-neutral-700 dark:bg-neutral-900"
      >
        <span className="mx-auto flex h-10 w-10 items-center justify-center rounded-full border border-dashed border-neutral-400/60 text-neutral-500 dark:border-neutral-600 dark:text-neutral-400">
          <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
            <path d="M12 8v4m0 4h.01" strokeLinecap="round" />
            <circle cx="12" cy="12" r="9" />
          </svg>
        </span>
        <h2 className="mt-3 text-sm font-semibold">No health data reported yet</h2>
        <p className="mx-auto mt-1 max-w-sm text-xs text-neutral-500 dark:text-neutral-400">
          The backend answered, but its health payload carried no status, version or dependency fields.
        </p>
        <div className="mt-4 flex justify-center">
          <RetryButton onClick={onRetry} isRefreshing={isRefreshing} />
        </div>
      </div>
    );
  }

  return (
    <article
      className={`rounded-xl border bg-white shadow-sm dark:bg-neutral-900 ${style.header}`}
    >
      <header className="flex items-center justify-between gap-3 border-b border-neutral-200 px-5 py-4 dark:border-neutral-800">
        <div className="flex items-center gap-3">
          <span className={`h-2.5 w-2.5 rounded-full ${style.dot}`} aria-hidden="true" />
          <h2 className="text-sm font-semibold">{style.label}</h2>
        </div>
        <time className="font-mono text-xs text-neutral-500 dark:text-neutral-400">
          {state.fetchedAt.toTimeString().slice(0, 8)}
        </time>
      </header>
      <dl className="px-5 py-4 text-sm">
        {state.health.status !== null && (
          <DetailRow label="Status" value={state.health.status} mono />
        )}
        {state.health.version !== null && (
          <DetailRow label="Version" value={state.health.version} mono />
        )}
        {Object.entries(state.health.details).map(([key, value]) => (
          <DetailRow key={key} label={key} value={formatDetailValue(value)} mono />
        ))}
      </dl>
      <footer className="border-t border-neutral-200 px-5 py-3 dark:border-neutral-800">
        <RetryButton onClick={onRetry} isRefreshing={isRefreshing} small />
      </footer>
    </article>
  );
}

function DetailRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1">
      <dt className="shrink-0 text-neutral-500 dark:text-neutral-400">{label}</dt>
      <dd className={`break-all text-right ${mono ? "font-mono text-xs leading-5" : ""}`}>{value}</dd>
    </div>
  );
}

function RetryButton({
  onClick,
  isRefreshing,
  small,
}: {
  onClick: () => void;
  isRefreshing: boolean;
  small?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={isRefreshing}
      className={`inline-flex items-center gap-2 rounded-md border border-neutral-300 bg-white px-3 font-medium text-neutral-800 transition-colors hover:bg-neutral-100 disabled:cursor-not-allowed disabled:opacity-60 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100 dark:hover:bg-neutral-700 ${
        small ? "py-1 text-xs" : "py-1.5 text-sm"
      }`}
    >
      <svg
        viewBox="0 0 24 24"
        className={`h-3.5 w-3.5 ${isRefreshing ? "animate-spin" : ""}`}
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        aria-hidden="true"
      >
        <path d="M21 12a9 9 0 1 1-2.64-6.36" strokeLinecap="round" />
        <path d="M21 3v6h-6" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      {isRefreshing ? "Refreshing…" : "Refresh now"}
    </button>
  );
}

function formatDetailValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value === null || value === undefined) return "—";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
