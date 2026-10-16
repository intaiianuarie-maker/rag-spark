"use client";

import { useParams } from "next/navigation";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

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
  created_at?: string;
  updated_at?: string;
  goal?: string | null;
  output?: string | null;
  budget?: {
    max_steps: number;
    max_tokens: number;
    max_tool_calls: number;
    max_cost_usd?: number | null;
    max_tool_cost_usd?: number | null;
    max_wall_clock_s?: number | null;
  } | null;
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    tool_calls: number;
    cost_usd: number;
  } | null;
  sources?: Array<{
    document_id?: string | null;
    chunk_id?: string;
    statement?: string | null;
    verdict?: string | null;
    quote?: string;
  }>;
};

export type StepEvent = {
  run_id: string;
  sequence: number;
  step_type: string;
  action_summary: string;
  tool_name?: string | null;
  tool_input?: Record<string, unknown> | null;
  tool_output?: Record<string, unknown> | null;
  error?: string | null;
  error_code?: string | null;
  status: string;
  created_at: string;
  latency_ms?: number | null;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    tool_calls?: number;
    cost_usd?: number;
  } | null;
};

export type HistoryResponse = {
  run_id: string;
  workspace_id: string;
  steps: StepEvent[];
  total_steps: number;
};

export type ApprovalResponse = {
  run_id: string;
  workspace_id: string;
  status: string;
  decision: string;
  error?: string | null;
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

export type ChipTone =
  | "pending"
  | "running"
  | "success"
  | "danger"
  | "neutral"
  | "partial";

const STATUS_TONES: Record<RunStatusValue, ChipTone> = {
  queued: "pending",
  running: "running",
  waiting_input: "pending",
  waiting_approval: "pending",
  completed: "success",
  // F-2 (T-V3-031): `partial` is TERMINAL, so it must not wear the in-progress
  // amber tone. It gets its own violet tone — see the list page for the
  // reasoning. Matches the detail page's own terminal-status set below.
  partial: "partial",
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
  partial: "border-violet-600/30 bg-violet-100 text-violet-900 dark:bg-violet-900/50 dark:text-violet-200",
};

/** Pure tone lookup (exported for unit tests, mirroring knowledge/status-chip). */
export function runStatusTone(status: RunStatusValue | string): ChipTone {
  return STATUS_TONES[status as RunStatusValue] ?? "neutral";
}

export function RunStatusChip({ status }: { status: RunStatusValue | string }) {
  const tone = runStatusTone(status);
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
  const [steps, setSteps] = useState<StepEvent[]>([]);
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
  const reqRef = useRef(0);
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  if (key !== loadedKey) {
    setLoadedKey(key);
    setHistory((current) => timelineAfterKeyChange(loadedKey, key, current));
    setSnapshot(null);
    setRecord(null);
    setSteps([]);
    setNotFound(false);
    setError(null);
  }

  const load = useCallback(async () => {
    if (workspaceId === null || runId === null) return;
    const tok = ++reqRef.current;
    try {
      const [next, hist] = await Promise.all([
        auth.authFetch<RunSnapshot>(
          `/v1/workspaces/${workspaceId}/agent/runs/${runId}`,
        ),
        auth
          .authFetch<HistoryResponse>(
            `/v1/workspaces/${workspaceId}/agent/runs/${runId}/history`,
          )
          .catch(() => null),
      ]);
      if (reqRef.current !== tok) return;
      setSnapshot(next);
      if (hist && Array.isArray(hist.steps)) {
        setSteps(hist.steps);
      }
      setNotFound(false);
      setError(null);
      setRecord(loadRecord(workspaceId, runId));
      setHistory((current) => appendTransition(current, next));
    } catch (fetchError: unknown) {
      if (reqRef.current !== tok) return;
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

  const handleApproval = useCallback(
    async (decision: "approved" | "rejected") => {
      if (workspaceId === null || runId === null) return;
      setBusy(true);
      setError(null);
      try {
        await auth.authFetch<ApprovalResponse>(
          `/v1/workspaces/${workspaceId}/agent/runs/${runId}/approval`,
          {
            method: "POST",
            body: { decision },
          },
        );
        void load();
      } catch (appError: unknown) {
        const message =
          appError instanceof Error ? appError.message : "Could not submit approval decision.";
        setError(message);
      } finally {
        setBusy(false);
      }
    },
    [workspaceId, runId, load],
  );

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

      {/* Human approval gate (T-R15/T-R17) */}
      {snapshot?.status === "waiting_approval" ? (
        <section
          aria-labelledby="approval-heading"
          className="mt-6 rounded-md border border-amber-600/30 bg-amber-50 p-4 dark:border-amber-500/30 dark:bg-amber-950/30"
        >
          <h2
            id="approval-heading"
            className="text-sm font-semibold text-amber-900 dark:text-amber-200"
          >
            Human approval required
          </h2>
          <p className="mt-1 text-sm text-amber-800 dark:text-amber-300">
            The agent has paused and requires human approval before proceeding with a tool action.
          </p>
          <div className="mt-3 flex gap-2">
            <Button
              size="sm"
              onClick={() => void handleApproval("approved")}
              disabled={busy}
              className="bg-emerald-700 text-white hover:bg-emerald-800 dark:bg-emerald-600"
            >
              {busy ? "Working…" : "Approve action"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void handleApproval("rejected")}
              disabled={busy}
              className="border-red-600 text-red-700 hover:bg-red-50 dark:border-red-400 dark:text-red-300"
            >
              Reject action
            </Button>
          </div>
        </section>
      ) : null}

      {/* Real Output (T-R17: 0 mock fields) */}
      <section aria-labelledby="output-heading" className="mt-6">
        <h2
          id="output-heading"
          className="text-sm font-medium text-neutral-900 dark:text-neutral-100"
        >
          Output
        </h2>
        {snapshot?.output ? (
          <div className="mt-2 rounded-md border border-neutral-200 bg-neutral-50 p-4 font-mono text-sm whitespace-pre-wrap dark:border-neutral-800 dark:bg-neutral-900">
            {snapshot.output}
          </div>
        ) : terminal ? (
          <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">
            No output produced by this run.
          </p>
        ) : (
          <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">
            Run in progress — output will appear upon completion.
          </p>
        )}
      </section>

      {/* Budget + Real Usage (T-R17) */}
      <section aria-labelledby="budget-heading" className="mt-6">
        <h2
          id="budget-heading"
          className="text-sm font-medium text-neutral-900 dark:text-neutral-100"
        >
          Budget & Usage
        </h2>
        {snapshot?.budget ? (
          <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
            Limits: {snapshot.budget.max_tokens.toLocaleString()} tokens · {snapshot.budget.max_steps} steps ·{" "}
            {snapshot.budget.max_tool_calls} tool calls
            {snapshot.budget.max_cost_usd != null ? ` · $${snapshot.budget.max_cost_usd.toFixed(4)} max` : ""}
          </p>
        ) : record ? (
          <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
            Limits: {record.max_tokens.toLocaleString()} tokens · {record.max_steps} steps ·{" "}
            {record.max_tool_calls} tool calls
          </p>
        ) : (
          <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
            Default workspace limits applied.
          </p>
        )}
        {snapshot?.usage ? (
          <div className="mt-2 grid grid-cols-2 gap-2 rounded-md border border-neutral-200 p-2.5 text-xs sm:grid-cols-4 dark:border-neutral-800">
            <div>
              <span className="text-neutral-500">Prompt tokens:</span>{" "}
              <span className="font-medium">{snapshot.usage.prompt_tokens.toLocaleString()}</span>
            </div>
            <div>
              <span className="text-neutral-500">Completion tokens:</span>{" "}
              <span className="font-medium">{snapshot.usage.completion_tokens.toLocaleString()}</span>
            </div>
            <div>
              <span className="text-neutral-500">Tool calls:</span>{" "}
              <span className="font-medium">{snapshot.usage.tool_calls}</span>
            </div>
            <div>
              <span className="text-neutral-500">Total cost:</span>{" "}
              <span className="font-medium">${snapshot.usage.cost_usd.toFixed(5)}</span>
            </div>
          </div>
        ) : null}
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

      {/* Real Sources (T-R17) */}
      {snapshot?.sources && snapshot.sources.length > 0 ? (
        <section aria-labelledby="sources-heading" className="mt-6">
          <h2
            id="sources-heading"
            className="text-sm font-medium text-neutral-900 dark:text-neutral-100"
          >
            Sources ({snapshot.sources.length})
          </h2>
          <ul className="mt-2 space-y-2">
            {snapshot.sources.map((src, idx) => (
              <li
                key={idx}
                className="rounded-md border border-neutral-200 p-3 text-sm dark:border-neutral-800"
              >
                {src.statement ? (
                  <p className="font-medium text-neutral-900 dark:text-neutral-100">
                    {src.statement}
                  </p>
                ) : null}
                {src.quote ? (
                  <blockquote className="mt-1 text-xs italic text-neutral-600 dark:text-neutral-400">
                    &ldquo;{src.quote}&rdquo;
                  </blockquote>
                ) : null}
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {src.document_id ? (
                    <Link
                      href={`/knowledge/documents/${src.document_id}${src.chunk_id ? `/${src.chunk_id}` : ""}`}
                      className="inline-flex items-center rounded bg-neutral-100 px-2 py-0.5 font-mono text-xs text-blue-700 hover:underline dark:bg-neutral-800 dark:text-blue-300"
                    >
                      Doc: {src.document_id.slice(0, 8)}
                      {src.chunk_id ? `#${src.chunk_id.slice(0, 8)}` : ""}
                    </Link>
                  ) : null}
                  {src.verdict ? (
                    <Badge variant="outline" className="text-[10px]">
                      {src.verdict}
                    </Badge>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {/* Real Steps (T-R17: from history endpoint) */}
      <section aria-labelledby="steps-heading" className="mt-6">
        <h2
          id="steps-heading"
          className="text-sm font-medium text-neutral-900 dark:text-neutral-100"
        >
          Execution Steps ({steps.length})
        </h2>
        {steps.length === 0 ? (
          <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">
            {terminal ? "No execution steps recorded." : "No steps recorded yet."}
          </p>
        ) : (
          <ol className="mt-2 space-y-2">
            {steps.map((st) => (
              <li
                key={st.sequence}
                className="rounded-md border border-neutral-200 p-3 dark:border-neutral-800"
              >
                <div className="flex items-center justify-between text-xs text-neutral-500">
                  <span className="font-mono">
                    Step #{st.sequence} · {st.step_type}
                  </span>
                  <span>{st.latency_ms != null ? `${st.latency_ms}ms` : ""}</span>
                </div>
                <div className="mt-1 text-sm font-medium text-neutral-900 dark:text-neutral-100">
                  {st.action_summary || st.tool_name || "Step executed"}
                </div>
                {st.tool_name ? (
                  <div className="mt-0.5 text-xs text-neutral-600 dark:text-neutral-400">
                    Tool: <code className="font-mono">{st.tool_name}</code>
                  </div>
                ) : null}
                {st.error ? (
                  <div className="mt-1 text-xs text-red-600 dark:text-red-400">
                    {st.error}
                  </div>
                ) : null}
              </li>
            ))}
          </ol>
        )}
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
