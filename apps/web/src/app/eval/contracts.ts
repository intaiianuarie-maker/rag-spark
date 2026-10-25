"use client";

import { auth } from "@/lib/auth-client";

export type EvalExample = {
  question: string;
  expected_answer?: string;
  expected_sources?: string[];
  tags?: string[];
};

export type EvalDataset = {
  id: string;
  workspace_id?: string | null;
  name: string;
  examples: EvalExample[];
};

export type EvalConfig = {
  mode?: "hybrid" | "semantic" | "keyword";
  top_k?: number;
};

export type EvalRun = {
  run_id: string;
  dataset_id: string;
  n_questions: number;
  results: Record<string, number>;
  cost: number;
  latency_ms: number;
  tokens: number;
  config_snapshot: Record<string, unknown>;
  accounting_complete: boolean;
};

export type EvalComparison = {
  baseline_run_id: string;
  candidate_run_id: string;
  dimension: string | null;
  metric_deltas: { metric: string; baseline: number; candidate: number; delta: number; direction: string; higher_is_better: boolean }[];
  cost_delta: number;
  latency_delta_ms: number;
  tokens_delta: number;
  cost_direction: string;
  latency_direction: string;
  winners: Record<string, string>;
  configuration_validated: boolean;
};

export async function listEvalDatasets(workspaceId: string): Promise<EvalDataset[]> {
  return auth.authFetch<EvalDataset[]>(`/v1/workspaces/${workspaceId}/eval/datasets`, {});
}

export async function createEvalDataset(
  workspaceId: string,
  name: string,
  examples: EvalExample[],
): Promise<EvalDataset> {
  return auth.authFetch<EvalDataset>(`/v1/workspaces/${workspaceId}/eval/datasets`, {
    method: "POST",
    body: { name, examples },
  });
}

export async function getEvalDataset(workspaceId: string, id: string): Promise<EvalDataset> {
  return auth.authFetch<EvalDataset>(`/v1/workspaces/${workspaceId}/eval/datasets/${id}`, {});
}

export async function listEvalRuns(workspaceId: string): Promise<EvalRun[]> {
  return auth.authFetch<EvalRun[]>(`/v1/workspaces/${workspaceId}/eval/runs`, {});
}

export async function getEvalRun(workspaceId: string, id: string): Promise<EvalRun> {
  return auth.authFetch<EvalRun>(`/v1/workspaces/${workspaceId}/eval/runs/${id}`, {});
}

export async function runEval(
  workspaceId: string,
  body: { dataset_id?: string; name?: string; examples?: EvalExample[]; config?: EvalConfig },
): Promise<EvalRun> {
  return auth.authFetch<EvalRun>(`/v1/workspaces/${workspaceId}/eval/runs`, {
    method: "POST",
    body,
  });
}

export async function replayEvalRun(workspaceId: string, runId: string): Promise<EvalRun> {
  return auth.authFetch<EvalRun>(`/v1/workspaces/${workspaceId}/eval/runs/${runId}/replay`, {
    method: "POST",
  });
}

export async function compareEvalRuns(
  workspaceId: string,
  baselineRunId: string,
  candidateRunId: string,
  dimension: string,
): Promise<EvalComparison> {
  return auth.authFetch<EvalComparison>(`/v1/workspaces/${workspaceId}/eval/comparisons`, {
    method: "POST",
    body: { baseline_run_id: baselineRunId, candidate_run_id: candidateRunId, dimension },
  });
}

/** Format a numeric score for display (4 decimals). Pure, side-effect free. */
export function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toFixed(4);
}

/** Whether source text may be shown: opt-in only, default off (privacy). */
export function mayShowSourceText(optIn: boolean): boolean {
  return optIn === true;
}
