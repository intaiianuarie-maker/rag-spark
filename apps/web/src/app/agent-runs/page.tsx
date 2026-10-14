"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { auth } from "@/lib/auth-client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useWorkspaces } from "@/components/workspace/workspace-provider";

/**
 * Agent run list (T-V3-023) — list + start over the T-V3-021 run routes.
 *
 * Data pattern mirrors `app/ask/page.tsx` + `app/search/page.tsx`: same-origin
 * proxy calls through the typed auth client (`auth.authFetch`), the workspace
 * scope from `useWorkspaces()`, and error/empty states as first-class rendered
 * states rather than colour-only affordances.
 *
 * Run visibility: the API exposes start/status/cancel but no server-side list,
 * so this page keeps a browser-scoped registry (localStorage, per workspace)
 * of the runs this browser started. Each row still renders LIVE status from
 * GET /agent/runs/{id}; the local record only supplies the goal and the
 * limits the run was started with.
 *
 * No hidden rendering: every visible field is a typed wire field (run_id,
 * goal, status, error, error_code) or a limit the caller set. Nothing on
 * this page renders free-form model output.
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

/** GET /agent/runs/{id} and POST /agent/runs responses (identical shape). */
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

export type ReadinessInfo = {
  workspace_id: string;
  ready: boolean;
  status: string;
  missing: string | null;
  detail: string | null;
};

/** POST /agent/runs request body. */
export type StartRunBody = {
  goal: string;
  max_steps: number;
  max_tokens: number;
  max_tool_calls: number;
};

/** Browser-local record: what this browser started, with the limits it set. */
export type RunRecord = RunSnapshot & {
  goal: string;
  max_steps: number;
  max_tokens: number;
  max_tool_calls: number;
  created_at: string;
};

/**
 * Merges server-authoritative runs with local browser records.
 * Server records provide authoritative status, output, budget, usage, and sources.
 * Local records supply local start timestamps or fallback limit values if absent on server.
 */
export function mergeRunRecords(
  serverRuns: RunSnapshot[],
  localRuns: RunRecord[],
): RunRecord[] {
  const localMap = new Map<string, RunRecord>();
  for (const local of localRuns) {
    localMap.set(local.run_id, local);
  }

  const merged: RunRecord[] = [];
  const seenIds = new Set<string>();

  for (const s of serverRuns) {
    seenIds.add(s.run_id);
    const local = localMap.get(s.run_id);
    merged.push({
      run_id: s.run_id,
      workspace_id: s.workspace_id,
      status: s.status,
      error: s.error,
      error_code: s.error_code,
      goal: s.goal || local?.goal || "Agent run",
      max_steps: s.budget?.max_steps ?? local?.max_steps ?? DEFAULT_LIMITS.max_steps,
      max_tokens: s.budget?.max_tokens ?? local?.max_tokens ?? DEFAULT_LIMITS.max_tokens,
      max_tool_calls:
        s.budget?.max_tool_calls ?? local?.max_tool_calls ?? DEFAULT_LIMITS.max_tool_calls,
      created_at: s.created_at || local?.created_at || new Date().toISOString(),
      output: s.output ?? null,
      budget: s.budget ?? null,
      usage: s.usage ?? null,
      sources: s.sources ?? [],
    });
  }

  for (const local of localRuns) {
    if (!seenIds.has(local.run_id)) {
      merged.push(local);
    }
  }

  return merged;
}

/** Error-code copy: the taxonomy surfaced as plain text, never just colour. */
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

const DEFAULT_LIMITS = { max_steps: 5, max_tokens: 100000, max_tool_calls: 50 };

/**
 * F-3 (T-V3-032): a CLEARED or non-numeric limit field falls back to the
 * DEFAULT for THAT field — including max_tool_calls.
 *
 * Previously max_steps/max_tokens fell back but max_tool_calls did not, so a
 * cleared tool-call field started a run with 0 (no tool calls at all) with no
 * warning: a silent tool-less run, which is the class of thing this page
 * refuses elsewhere.
 *
 * Deliberate precision: the fallback fires for blank and non-numeric input
 * only. A parsed 0 is KEPT, because the API accepts max_tool_calls=0
 * (router.py: ge=0) — a caller who clears the field gets the default, and a
 * caller who means zero tools still gets zero tools. That distinction is why
 * this is a helper rather than the old `|| DEFAULT` trick, which also turned a
 * legitimate 0 into 5.
 *
 * Exported and pure so the fallback is testable without a DOM harness (there
 * is none in this repo).
 */
export function startRunLimit(raw: string, fallback: number): number {
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function storageKey(workspaceId: string): string {
  return `agent-runs:v1:${workspaceId}`;
}

/** Read the browser-scoped registry. Never throws: storage can be unavailable. */
function loadRecords(workspaceId: string): RunRecord[] {
  try {
    const raw = window.localStorage.getItem(storageKey(workspaceId));
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (item): item is RunRecord =>
        typeof item === "object" &&
        item !== null &&
        typeof (item as RunRecord).run_id === "string",
    );
  } catch {
    return [];
  }
}

function saveRecords(workspaceId: string, records: RunRecord[]): void {
  try {
    window.localStorage.setItem(storageKey(workspaceId), JSON.stringify(records));
  } catch {
    // Storage unavailable (private mode): the list stays in memory this session.
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
  // amber tone. It gets its own violet tone: not pending (amber), not
  // fully-succeeded (emerald), not an error (red), not inert (grey) — it read
  // as "still working" before, which is exactly the wrong message for a run
  // that has already stopped with some of its work done.
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

/** Status chip — the status WORD is always rendered (never colour-only). */
export function RunStatusChip({ status }: { status: RunStatusValue | string }) {
  const tone = runStatusTone(status);
  return (
    <Badge variant="outline" className={`font-medium ${TONE_CLASSES[tone]}`}>
      {status}
    </Badge>
  );
}

/** Plain-text explanation for an error code — text, tone is secondary. */
export function errorCodeLabel(code: RunErrorCode | null): string | null {
  return code ? ERROR_CODE_LABELS[code] : null;
}

export default function AgentRunsPage() {
  const { active } = useWorkspaces();
  const workspaceId = active?.id ?? null;

  const [records, setRecords] = useState<RunRecord[]>([]);
  const [goal, setGoal] = useState("");
  const [maxSteps, setMaxSteps] = useState(String(DEFAULT_LIMITS.max_steps));
  const [maxTokens, setMaxTokens] = useState(String(DEFAULT_LIMITS.max_tokens));
  const [maxToolCalls, setMaxToolCalls] = useState(String(DEFAULT_LIMITS.max_tool_calls));
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [readiness, setReadiness] = useState<ReadinessInfo | null>(null);

  // The browser registry is derived during render on workspace change (the
  // React "adjust state when a prop changes" pattern) so no setState fires
  // synchronously inside an effect; the interval below only refreshes.
  const [loadedWorkspace, setLoadedWorkspace] = useState<string | null>(null);
  if (workspaceId !== loadedWorkspace) {
    setLoadedWorkspace(workspaceId);
    setRecords(workspaceId === null ? [] : loadRecords(workspaceId));
  }

  const checkReadiness = useCallback(async (workspace: string) => {
    try {
      const info = await auth.authFetch<ReadinessInfo>(
        `/v1/workspaces/${workspace}/agent/readiness`,
      );
      setReadiness(info);
    } catch {
      setReadiness(null);
    }
  }, []);

  /**
   * One refresh pass: fetch live server runs and merge with local records.
   * If server listing fails, fall back to polling per known local record.
   */
  const runRefreshPass = useCallback((workspace: string) => {
    void (async () => {
      try {
        const serverRuns = await auth.authFetch<RunSnapshot[]>(
          `/v1/workspaces/${workspace}/agent/runs`,
        );
        const locals = loadRecords(workspace);
        const merged = mergeRunRecords(serverRuns, locals);
        setRecords(merged);
        saveRecords(workspace, merged);
      } catch {
        const current = loadRecords(workspace);
        if (current.length === 0) return;
        const live = await Promise.all(
          current.map(async (record) => {
            try {
              const snapshot = await auth.authFetch<RunSnapshot>(
                `/v1/workspaces/${workspace}/agent/runs/${record.run_id}`,
              );
              return { ...record, ...snapshot, goal: snapshot.goal || record.goal };
            } catch {
              return record;
            }
          }),
        );
        setRecords(live);
      }
    })();
  }, []);

  const refresh = useCallback(() => {
    if (workspaceId !== null) {
      void checkReadiness(workspaceId);
      runRefreshPass(workspaceId);
    }
  }, [workspaceId, checkReadiness, runRefreshPass]);

  useEffect(() => {
    if (workspaceId === null) return;
    const initialTimer = window.setTimeout(() => {
      void checkReadiness(workspaceId);
      runRefreshPass(workspaceId);
    }, 0);
    const timer = window.setInterval(() => runRefreshPass(workspaceId), 4000);
    return () => {
      window.clearTimeout(initialTimer);
      window.clearInterval(timer);
    };
  }, [workspaceId, checkReadiness, runRefreshPass]);

  const handleStart = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      if (workspaceId === null || goal.trim().length === 0) return;
      if (readiness !== null && !readiness.ready) return;
      setStarting(true);
      setStartError(null);
      try {
        const body: StartRunBody = {
          goal: goal.trim(),
          max_steps: startRunLimit(maxSteps, DEFAULT_LIMITS.max_steps),
          max_tokens: startRunLimit(maxTokens, DEFAULT_LIMITS.max_tokens),
          max_tool_calls: startRunLimit(maxToolCalls, DEFAULT_LIMITS.max_tool_calls),
        };
        const snapshot = await auth.authFetch<RunSnapshot>(
          `/v1/workspaces/${workspaceId}/agent/runs`,
          { method: "POST", body },
        );
        const record: RunRecord = {
          ...snapshot,
          goal: body.goal,
          max_steps: body.max_steps,
          max_tokens: body.max_tokens,
          max_tool_calls: body.max_tool_calls,
          created_at: new Date().toISOString(),
        };
        const next = [record, ...loadRecords(workspaceId)];
        saveRecords(workspaceId, next);
        setRecords(next);
        setGoal("");
      } catch (error: unknown) {
        const message = error instanceof Error ? error.message : "Could not start the run.";
        setStartError(message);
      } finally {
        setStarting(false);
      }
    },
    [workspaceId, goal, maxSteps, maxTokens, maxToolCalls, readiness],
  );

  if (workspaceId === null) {
    return (
      <main className="px-6 py-8 md:px-8">
        <h1 className="text-lg font-semibold text-neutral-900 dark:text-neutral-100">
          Agent runs
        </h1>
        <p role="status" className="mt-4 text-sm text-neutral-600 dark:text-neutral-400">
          No workspace selected. Pick a workspace to see its agent runs.
        </p>
      </main>
    );
  }

  const isUnavailable = readiness !== null && !readiness.ready;

  return (
    <main className="px-6 py-8 md:px-8">
      <h1 className="text-lg font-semibold text-neutral-900 dark:text-neutral-100">
        Agent runs
      </h1>

      {/* Honest unavailable banner (T-R02/T-R17) */}
      {isUnavailable ? (
        <section
          role="alert"
          aria-label="Agent runtime unavailable"
          className="mt-4 rounded-md border border-amber-600/30 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-400/30 dark:bg-amber-950/40 dark:text-amber-200"
        >
          <div className="font-semibold">Agent execution unavailable</div>
          <p className="mt-1 text-xs">
            {readiness?.detail ||
              `Agent runs are unavailable in this workspace. No LLM provider is configured (missing: ${readiness?.missing || "llm_provider"}).`}
          </p>
        </section>
      ) : null}

      {/* Start a run — the limits are set here and rendered on every row. */}
      <section aria-labelledby="start-run-heading" className="mt-4 max-w-2xl">
        <h2
          id="start-run-heading"
          className="text-sm font-medium text-neutral-900 dark:text-neutral-100"
        >
          Start a run
        </h2>
        <form onSubmit={handleStart} className="mt-2 space-y-3">
          <div>
            <Label htmlFor="run-goal">Goal</Label>
            <Input
              id="run-goal"
              value={goal}
              onChange={(event) => setGoal(event.target.value)}
              placeholder="Summarise the Q3 incident findings"
              maxLength={4096}
              disabled={isUnavailable}
            />
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div>
              <Label htmlFor="run-max-steps">Max steps</Label>
              <Input
                id="run-max-steps"
                type="number"
                min={1}
                max={20}
                value={maxSteps}
                onChange={(event) => setMaxSteps(event.target.value)}
                disabled={isUnavailable}
              />
            </div>
            <div>
              <Label htmlFor="run-max-tokens">Max tokens</Label>
              <Input
                id="run-max-tokens"
                type="number"
                min={1}
                value={maxTokens}
                onChange={(event) => setMaxTokens(event.target.value)}
                disabled={isUnavailable}
              />
            </div>
            <div>
              <Label htmlFor="run-max-tool-calls">Max tool calls</Label>
              <Input
                id="run-max-tool-calls"
                type="number"
                min={0}
                value={maxToolCalls}
                onChange={(event) => setMaxToolCalls(event.target.value)}
                disabled={isUnavailable}
              />
            </div>
          </div>
          <Button
            type="submit"
            disabled={starting || goal.trim().length === 0 || isUnavailable}
          >
            {starting ? "Starting…" : isUnavailable ? "Unavailable" : "Start run"}
          </Button>
        </form>
        {startError ? (
          <p
            role="alert"
            className="mt-2 rounded-md border border-red-600/30 bg-red-100 px-3 py-2 text-sm text-red-900 dark:bg-red-900/50 dark:text-red-200"
          >
            {startError}
          </p>
        ) : null}
      </section>

      {/* Run list */}
      <section aria-labelledby="runs-heading" className="mt-8">
        <div className="flex items-center justify-between">
          <h2
            id="runs-heading"
            className="text-sm font-medium text-neutral-900 dark:text-neutral-100"
          >
            Runs
          </h2>
          <Button variant="outline" size="sm" onClick={() => refresh()}>
            Refresh
          </Button>
        </div>

        {records.length === 0 ? (
          <p
            role="status"
            className="mt-3 rounded-md border border-neutral-300 px-3 py-4 text-sm text-neutral-600 dark:border-neutral-700 dark:text-neutral-400"
          >
            No runs yet for this workspace. Runs started from this browser appear here;
            empty means nothing has been started, not that the service is down.
          </p>
        ) : (
          <ul className="mt-3 space-y-2">
            {records.map((record) => {
              const label = errorCodeLabel(record.error_code);
              return (
                <li
                  key={record.run_id}
                  className="rounded-md border border-neutral-200 p-3 dark:border-neutral-800"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <RunStatusChip status={record.status} />
                    <span
                      className="font-mono text-xs text-neutral-500 dark:text-neutral-400"
                      title={record.run_id}
                    >
                      {record.run_id.slice(0, 8)}
                    </span>
                    <Link
                      href={`/agent-runs/${record.run_id}`}
                      className="text-sm text-blue-700 underline dark:text-blue-300"
                    >
                      Open detail
                    </Link>
                  </div>
                  <p className="mt-1 text-sm text-neutral-900 dark:text-neutral-100">
                    {record.goal}
                  </p>
                  {/* Limits visible, not hidden. */}
                  <p className="mt-1 text-xs text-neutral-500 dark:text-neutral-400">
                    Limits: {record.max_tokens.toLocaleString()} tokens ·{" "}
                    {record.max_steps} steps · {record.max_tool_calls} tool calls
                  </p>
                  {record.output ? (
                    <p className="mt-1 line-clamp-2 text-xs text-neutral-700 dark:text-neutral-300 font-mono">
                      Output: {record.output}
                    </p>
                  ) : null}
                  {label ? (
                    <p className="mt-1 text-xs text-red-700 dark:text-red-300">{label}</p>
                  ) : null}
                  {record.error && !label ? (
                    <p className="mt-1 text-xs text-red-700 dark:text-red-300">{record.error}</p>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </main>
  );
}
