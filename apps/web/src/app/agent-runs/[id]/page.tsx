"use client";

import { useParams } from "next/navigation";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { auth } from "@/lib/auth-client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useWorkspaces } from "@/components/workspace/workspace-provider";

/**
 * Agent run detail + timeline (T-V3-023) — live status over the T-V3-021 routes.
 *
 * Same client pattern as the list page (auth proxy + workspace scope); every
 * rendered field is typed wire data (status / error / error_code / limits set
 * at start). The timeline shows the lifecycle states OBSERVED FOR THIS RUN IN
 * THIS BROWSER SESSION, labelled as such — the API exposes the current status
 * only, so the page does not invent transition history it was not given.
 *
 * Cancel is offered through the same POST /agent/runs/{id}/cancel route the
 * API ships; a cancelled or failed run reads as a clear outcome with its error
 * code explained in text, never as a colour-only state.
 */

/* ------------------------------------------------------------------ *
 * Wire shapes — mirror of apps/api/app/agents/router.py (T-V3-021).
 * Kept local: this ticket's allowed files are these two pages only.
 * ------------------------------------------------------------------ */

export type RunStatusValue =
  | "queued"
  | "running"
  | "waiting_input"
  | "waiting_approval"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled"
  | "limit_exceeded";

export type RunErrorCode =
  | "tool_not_allowed"
  | "tool_not_found"
  | "tool_timeout"
  | "tool_output_too_large"
  | "tool_schema_invalid"
  | "budget_exceeded"
  | "deadline_exceeded"
  | "cancelled"
  | "provider_error"
  | "schema_invalid"
  | "workspace_mismatch"
  | "source_revoked"
  | "unknown_required";

export type RunSnapshot = {
  run_id: string;
  workspace_id: string;
  status: RunStatusValue;
  error: string | null;
  error_code: RunErrorCode | null;
};

/** Browser-local record written by the list page when the run was started. */
export type RunRecord = {
  run_id: string;
  workspace_id: string;
  status: RunStatusValue;
  error: string | null;
  error_code: RunErrorCode | null;
  goal: string;
  max_steps: number;
  max_tokens: number;
  max_tool_calls: number;
  created_at: string;
};

const ERROR_CODE_LABELS: Record<RunErrorCode, string> = {
  tool_not_allowed: "Tool not allowed — a tool call was blocked by policy or approval.",
  tool_not_found: "Tool not found — the requested tool is not registered.",
  tool_timeout: "Tool timeout — a tool call exceeded its time limit.",
  tool_output_too_large: "Tool output too large — the result exceeded the size cap.",
  tool_schema_invalid: "Tool schema invalid — the arguments did not match the schema.",
  budget_exceeded: "Budget exhausted — the run stopped at its token or tool-call limit.",
  deadline_exceeded: "Deadline exceeded — the run stopped at its wall-clock limit.",
  cancelled: "Cancelled — the run was cancelled before completion.",
  provider_error: "Provider error — the model gateway failed.",
  schema_invalid: "Schema invalid — the requested action did not parse.",
  workspace_mismatch: "Workspace mismatch — the run belongs to another workspace.",
  source_revoked: "Source revoked — a cited source was deleted or superseded.",
  unknown_required: "Unknown failure — a required step failed for an unknown reason.",
};

const TERMINAL_STATUSES: ReadonlySet<RunStatusValue> = new Set<RunStatusValue>([
  "completed",
  "partial",
  "failed",
  "cancelled",
  "limit_exceeded",
]);

function storageKey(workspaceId: string): string {
  return `agent-runs:v1:${workspaceId}`;
}

function loadRecord(workspaceId: string, runId: string): RunRecord | null {
  try {
    const raw = window.localStorage.getItem(storageKey(workspaceId));
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    for (const item of parsed) {
      if (
        typeof item === "object" &&
        item !== null &&
        (item as RunRecord).run_id === runId
      ) {
        return item as RunRecord;
      }
    }
    return null;
  } catch {
    return null;
  }
}

type ChipTone = "pending" | "running" | "success" | "danger" | "neutral";

const STATUS_TONES: Record<RunStatusValue, ChipTone> = {
  queued: "pending",
  running: "running",
  waiting_input: "pending",
  waiting_approval: "pending",
  completed: "success",
  partial: "pending",
  failed: "danger",
  cancelled: "neutral",
  limit_exceeded: "danger",
};

const TONE_CLASSES: Record<ChipTone, string> = {
  pending: "border-amber-600/30 bg-amber-100 text-amber-900 dark:bg-amber-900/50 dark:text-amber-200",
  running: "border-blue-600/30 bg-blue-100 text-blue-900 dark:bg-blue-900/50 dark:text-blue-200",
  success: "border-emerald-600/30 bg-emerald-100 text-emerald-900 dark:bg-emerald-900/50 dark:text-emerald-200",
  danger: "border-red-600/30 bg-red-100 text-red-900 dark:bg-red-900/50 dark:text-red-200",
  neutral: "border-neutral-400/30 bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300",
};

export function RunStatusChip({ status }: { status: RunStatusValue | string }) {
  const tone = STATUS_TONES[status as RunStatusValue] ?? "neutral";
  return (
    <Badge variant="outline" className={`font-medium ${TONE_CLASSES[tone]}`}>
      {status}
    </Badge>
  );
}

/** One observed lifecycle state. `at` is when this browser saw it. */
export type TimelineEntry = {
  at: string;
  status: RunStatusValue;
  error_code: RunErrorCode | null;
};

/** Append only when the status actually changed — no synthetic transitions. */
function appendTransition(
  history: TimelineEntry[],
  snapshot: RunSnapshot,
): TimelineEntry[] {
  const last = history[history.length - 1];
  if (last && last.status === snapshot.status && last.error_code === snapshot.error_code) {
    return history;
  }
  return [
    ...history,
    {
      at: new Date().toISOString(),
      status: snapshot.status,
      error_code: snapshot.error_code,
    },
  ];
}

/**
 * F-1 (T-V3-030) — the timeline belongs to EXACTLY ONE (workspace, run).
 *
 * A key change means a different run is on screen, so the observed-state
 * history starts EMPTY: run A's lifecycle states must never render above run
 * B's, or the timeline reads as one continuous history for a run that never
 * had those states. Same key (a poll for the same run) keeps the history.
 *
 * Pure on purpose: this repo has no DOM test harness, so the decision is
 * testable as a function rather than only as source text.
 */
export function timelineAfterKeyChange(
  previousKey: string | null,
  nextKey: string | null,
  current: TimelineEntry[],
): TimelineEntry[] {
  return previousKey === nextKey ? current : [];
}

/** The per-run state key: workspace scope AND run id, or null while unknown. */
export function runStateKey(
  workspaceId: string | null,
  runId: string | null,
): string | null {
  if (workspaceId === null || runId === null) return null;
  return `${workspaceId}/${runId}`;
}

export default function AgentRunDetailPage() {
  const params = useParams();
  const runId = typeof params?.id === "string" ? params.id : null;
  const { active } = useWorkspaces();
  const workspaceId = active?.id ?? null;

  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [record, setRecord] = useState<RunRecord | null>(null);
  const [history, setHistory] = useState<TimelineEntry[]>([]);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // F-1 (T-V3-030): keyed per-run state, reset whenever the workspace or the
  // run id changes. React's "adjust state when a prop changes" pattern — the
  // same shape the list page uses (T-V3-023, `page.tsx`) — so an A→B run
  // navigation (or a workspace switch with a different run in the URL) starts
  // run B from empty instead of showing run A's observed states, status chip
  // and 404 alert. Polling the SAME run keeps its history (same key).
  const key = runStateKey(workspaceId, runId);
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  if (key !== loadedKey) {
    setLoadedKey(key);
    setHistory((current) => timelineAfterKeyChange(loadedKey, key, current));
    setSnapshot(null);
    setRecord(null);
    setNotFound(false);
    setError(null);
  }

  const load = useCallback(async () => {
    if (workspaceId === null || runId === null) return;
    try {
      const next = await auth.authFetch<RunSnapshot>(
        `/v1/workspaces/${workspaceId}/agent/runs/${runId}`,
      );
      setSnapshot(next);
      setNotFound(false);
      setError(null);
      setRecord(loadRecord(workspaceId, runId));
      setHistory((current) => appendTransition(current, next));
    } catch (fetchError: unknown) {
      const status =
        typeof fetchError === "object" && fetchError !== null && "status" in fetchError
          ? Number((fetchError as { status: unknown }).status)
          : 0;
      if (status === 404 || status === 403) {
        setNotFound(true);
        setSnapshot(null);
        return;
      }
      const message =
        fetchError instanceof Error ? fetchError.message : "Could not load the run.";
      setError(message);
    }
  }, [workspaceId, runId]);

  // Load on mount AND whenever the run's key changes (workspace or run id) —
  // the F-1 fix: the previous `loadedRef` guard ran this exactly once for the
  // component's lifetime, so navigating from run A to run B kept run A's
  // timeline (and its snapshot) on screen until a poll happened to fetch B.
  //
  // The read is scheduled as a TIMER CALLBACK rather than called in the effect
  // body: `load` sets state, and react-hooks/set-state-in-effect flags a
  // synchronous call here (the whole-src lint baseline is otherwise clean, so
  // this is the one line that would regress it). A 0 ms timeout is the
  // effect-sanctioned shape — it fires on the next tick, which is not
  // observable next to a network round-trip.
  useEffect(() => {
    if (key === null) return;
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load, key]);

  // Light polling while the run is active; stops at terminal statuses.
  useEffect(() => {
    if (snapshot === null || TERMINAL_STATUSES.has(snapshot.status)) return;
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
  }, [snapshot, load]);

  const handleCancel = useCallback(async () => {
    if (workspaceId === null || runId === null) return;
    setBusy(true);
    setError(null);
    try {
      const next = await auth.authFetch<RunSnapshot>(
        `/v1/workspaces/${workspaceId}/agent/runs/${runId}/cancel`,
        { method: "POST" },
      );
      setSnapshot(next);
      setHistory((current) => appendTransition(current, next));
    } catch (cancelError: unknown) {
      const message =
        cancelError instanceof Error ? cancelError.message : "Could not cancel the run.";
      setError(message);
    } finally {
      setBusy(false);
    }
  }, [workspaceId, runId]);

  if (runId === null) {
    return (
      <main className="px-6 py-8 md:px-8">
        <p role="alert" className="text-sm text-red-700 dark:text-red-300">
          No run id in the URL.
        </p>
      </main>
    );
  }

  if (workspaceId === null) {
    return (
      <main className="px-6 py-8 md:px-8">
        <h1 className="text-lg font-semibold text-neutral-900 dark:text-neutral-100">
          Agent run
        </h1>
        <p role="status" className="mt-4 text-sm text-neutral-600 dark:text-neutral-400">
          No workspace selected. Pick a workspace to view this run.
        </p>
      </main>
    );
  }

  const terminal = snapshot !== null && TERMINAL_STATUSES.has(snapshot.status);
  const errorLabel = snapshot?.error_code ? ERROR_CODE_LABELS[snapshot.error_code] : null;

  return (
    <main className="px-6 py-8 md:px-8">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold text-neutral-900 dark:text-neutral-100">
          Agent run
        </h1>
        <span className="font-mono text-xs text-neutral-500 dark:text-neutral-400">
          {runId}
        </span>
        {snapshot ? <RunStatusChip status={snapshot.status} /> : null}
      </div>
      <p className="mt-1 text-sm">
        <Link href="/agent-runs" className="text-blue-700 underline dark:text-blue-300">
          Back to all runs
        </Link>
      </p>

      {notFound ? (
        <p
          role="alert"
          className="mt-4 rounded-md border border-red-600/30 bg-red-100 px-3 py-2 text-sm text-red-900 dark:bg-red-900/50 dark:text-red-200"
        >
          Run {runId} was not found in this workspace. It may belong to another workspace
          or the id may be mistyped.
        </p>
      ) : null}

      {error ? (
        <p role="alert" className="mt-4 text-sm text-red-700 dark:text-red-300">
          {error}
        </p>
      ) : null}

      {/* Progress state: reads as in-progress, not as an error. */}
      {snapshot && !terminal ? (
        <p role="status" className="mt-4 text-sm text-neutral-600 dark:text-neutral-400">
          This run is {snapshot.status}. Status refreshes automatically.
        </p>
      ) : null}

      {/* Budget + limits — surfaced, not hidden. */}
      <section aria-labelledby="budget-heading" className="mt-6">
        <h2
          id="budget-heading"
          className="text-sm font-medium text-neutral-900 dark:text-neutral-100"
        >
          Budget
        </h2>
        {record ? (
          <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
            Limits: {record.max_tokens.toLocaleString()} tokens · {record.max_steps} steps ·{" "}
            {record.max_tool_calls} tool calls
          </p>
        ) : (
          <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
            This run was not started from this browser, so its limits are not recorded
            locally. Budget exhaustion is still reported in the outcome below.
          </p>
        )}
        {snapshot?.status === "limit_exceeded" && snapshot.error_code === "budget_exceeded" ? (
          <p className="mt-1 text-sm text-red-700 dark:text-red-300">
            The run exhausted its budget and stopped; it was not completed.
          </p>
        ) : null}
        {snapshot?.status === "limit_exceeded" && snapshot.error_code === "deadline_exceeded" ? (
          <p className="mt-1 text-sm text-red-700 dark:text-red-300">
            The run reached its wall-clock deadline and stopped.
          </p>
        ) : null}
      </section>

      {/* Outcome — failed/cancelled reads clearly, with the error code in text. */}
      {terminal && snapshot ? (
        <section aria-labelledby="outcome-heading" className="mt-6">
          <h2
            id="outcome-heading"
            className="text-sm font-medium text-neutral-900 dark:text-neutral-100"
          >
            Outcome
          </h2>
          <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
            Final status: <span className="font-medium">{snapshot.status}</span>
            {snapshot.error_code ? ` · ${snapshot.error_code}` : ""}
          </p>
          {errorLabel ? (
            <p className="mt-1 text-sm text-red-700 dark:text-red-300">{errorLabel}</p>
          ) : null}
          {snapshot.error && snapshot.error !== errorLabel ? (
            <p className="mt-1 text-xs text-neutral-500 dark:text-neutral-400">
              {snapshot.error}
            </p>
          ) : null}
        </section>
      ) : null}

      {/* Timeline — observed states for this run in this browser. */}
      <section aria-labelledby="timeline-heading" className="mt-6">
        <h2
          id="timeline-heading"
          className="text-sm font-medium text-neutral-900 dark:text-neutral-100"
        >
          Timeline
        </h2>
        <p className="mt-1 text-xs text-neutral-500 dark:text-neutral-400">
          Lifecycle states observed for this run in this browser session.
        </p>
        {history.length === 0 ? (
          <p role="status" className="mt-2 text-sm text-neutral-600 dark:text-neutral-400">
            No states observed yet.
          </p>
        ) : (
          <ol className="mt-2 space-y-1">
            {history.map((entry, index) => (
              <li key={`${entry.at}-${index}`} className="flex flex-wrap items-center gap-2 text-sm">
                <RunStatusChip status={entry.status} />
                {entry.error_code ? (
                  <span className="text-xs text-red-700 dark:text-red-300">
                    {entry.error_code}
                  </span>
                ) : null}
                <span className="font-mono text-xs text-neutral-500 dark:text-neutral-400">
                  {entry.at}
                </span>
              </li>
            ))}
          </ol>
        )}
      </section>

      {/* Cancel — same route the API ships; disabled once terminal. */}
      <section aria-labelledby="actions-heading" className="mt-6">
        <h2
          id="actions-heading"
          className="text-sm font-medium text-neutral-900 dark:text-neutral-100"
        >
          Actions
        </h2>
        <div className="mt-2 flex gap-2">
          <Button variant="outline" size="sm" onClick={() => void load()} disabled={busy}>
            Refresh
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void handleCancel()}
            disabled={busy || snapshot === null || terminal}
          >
            {busy ? "Working…" : "Cancel run"}
          </Button>
        </div>
        {terminal ? (
          <p className="mt-2 text-xs text-neutral-500 dark:text-neutral-400">
            This run has finished ({snapshot?.status}); it can no longer be cancelled.
          </p>
        ) : null}
      </section>
    </main>
  );
}
