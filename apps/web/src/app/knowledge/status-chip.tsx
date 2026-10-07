"use client";

import { Badge } from "@/components/ui/badge";
import type { JobState } from "./contracts";

/**
 * Status chip mapping for documents/jobs (TASK-M2-002a; unit-tested in
 * status-chip.test.ts — every known job state covered).
 */

export type ChipTone = "pending" | "running" | "success" | "danger" | "neutral";

const TONES: Record<ChipTone, string> = {
  pending: "border-amber-600/30 bg-amber-100 text-amber-900 dark:bg-amber-900/50 dark:text-amber-200",
  running: "border-blue-600/30 bg-blue-100 text-blue-900 dark:bg-blue-900/50 dark:text-blue-200",
  success: "border-emerald-600/30 bg-emerald-100 text-emerald-900 dark:bg-emerald-900/50 dark:text-emerald-200",
  danger: "border-red-600/30 bg-red-100 text-red-900 dark:bg-red-900/50 dark:text-red-200",
  neutral: "border-neutral-400/30 bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300",
};

const JOB_STATE_TONES: Record<JobState, ChipTone> = {
  queued: "pending",
  parsing: "running",
  chunking: "running",
  embedding: "running",
  ready: "success",
  failed: "danger",
};

/** Pure mapping, exported for unit tests and reuse by document rows. */
export function jobStateTone(state: string): ChipTone {
  const normalized = state.toLowerCase();
  if (normalized in JOB_STATE_TONES) {
    return JOB_STATE_TONES[normalized as JobState];
  }
  // Unknown/future backend states render neutrally instead of mis-colouring.
  return "neutral";
}

export function StatusChip({ state }: { state: string | null | undefined }) {
  const label = (state ?? "unknown").toLowerCase();
  return (
    <Badge variant="outline" className={`font-medium ${TONES[jobStateTone(label)]}`}>
      {label}
    </Badge>
  );
}

export function JobTimeline({ jobs }: { jobs: ReadonlyArray<{ id: string; state: string; error?: string | null; updated_at?: string | null }> }) {
  if (jobs.length === 0) {
    return <p className="text-xs text-neutral-500 dark:text-neutral-400">No ingestion jobs recorded yet.</p>;
  }
  return (
    <ol className="space-y-2">
      {jobs.map((job) => (
        <li key={job.id} className="flex flex-wrap items-center gap-2 text-sm">
          <StatusChip state={job.state} />
          <span className="font-mono text-xs text-neutral-500 dark:text-neutral-400">{job.id.slice(0, 8)}</span>
          {job.error ? (
            <span className="w-full text-xs text-red-700 dark:text-red-300">{job.error}</span>
          ) : null}
        </li>
      ))}
    </ol>
  );
}