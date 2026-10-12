"use client";

import { auth } from "@/lib/auth-client";

export type Citation = {
  chunk_id: string;
  document_id: string | null;
  filename: string | null;
  page: number | null;
  section: string | null;
  span: [number, number] | null;
  workspace_id: string | null;
};

export type Claim = {
  text: string;
  chunk_ids: string[];
};

export type Confidence = {
  level: "high" | "medium" | "low" | "unavailable";
  basis: string;
  probability?: number | null;
};

export type AnswerPayload = {
  answer: string;
  status: "answered" | "partial" | "insufficient_evidence" | "clarification_needed" | "unavailable";
  reason_code?: string | null;
  reason?: string | null;
  confidence?: Confidence;
  limitations?: string[];
  trace_id?: string | null;
  claims: Claim[];
  citations: Citation[];
};

/**
 * Typed wire shape for `POST /v1/workspaces/{workspace_id}/ask`.
 *
 * Serialization boundary (T-V1-008 / RP-06, QF-06): the helper hands this
 * **object** to `authFetch`, which owns the single object→JSON decision in
 * auth-client. The Ask path must never `JSON.stringify` on its own — that was
 * the double-serialization bug (a JSON string sent as a JSON string).
 */
export type AskRequest = {
  query: string;
  model?: string;
};

/** Build the typed Ask request body; drops an absent/empty model. */
export function buildAskBody(query: string, model?: string): AskRequest {
  return { query, ...(model ? { model } : {}) };
}

export async function askQuestion(
  workspaceId: string,
  query: string,
  model?: string,
): Promise<AnswerPayload> {
  return await auth.authFetch<AnswerPayload>(
    `/v1/workspaces/${workspaceId}/ask`,
    {
      method: "POST",
      body: buildAskBody(query, model),
    },
  );
}

/* --- TASK-Q0 additive contract, carried end-to-end by T-V1-025 ---
 * Single shared taxonomy lives in `packages/core/errors.py`; this file only
 * MIRRORS it. Purely additive exports; existing payload types above are
 * unchanged. Locked by tests/contracts/test_error_taxonomy_contract.py — a
 * drift between these unions and the Python registry fails the build.
 */

/** Answer `status` values — mirror of `packages/core.errors.AnswerStatus`
 * (re-exported by `packages/answer/models.py`). `degraded` = grounded answer
 * served with an optional capability down; `needs_review` = content accepted
 * from a partially-parsed source. */
export type AnswerStatus =
  | "answered"
  | "partial"
  | "insufficient_evidence"
  | "clarification_needed"
  | "unavailable"
  | "degraded"
  | "needs_review";

/** Ingestion job `status` values — mirror of `packages/core.errors.JobStatus`
 * (re-exported by `packages/ingestion/persistence.py`). `ready` remains the
 * only fully-published state; `degraded`/`partial`/`needs_review` are honest
 * disclosure states on top of it. */
export type JobStatus =
  | "queued"
  | "running"
  | "ready"
  | "failed"
  | "degraded"
  | "partial"
  | "needs_review";

/** Canonical reason codes the ask endpoint may emit in `reason_code`
 * (mirror of the ANSWER subset of `packages/core.errors.ReasonCode`).
 * `reason_code` stays `string | null` on the wire for compatibility. */
export type AnswerReasonCode =
  | "PARSE_FALLBACK"
  | "EVIDENCE_MISSING"
  | "NO_CLAIMS"
  | "CITATION_INVALID"
  | "PARTIAL_EVIDENCE"
  | "DEGRADED_RESULT";

/** Backing dependency names — mirror of `packages/core.errors.Dependency`. */
export type Dependency =
  | "postgres"
  | "redis"
  | "qdrant"
  | "minio"
  | "infinity"
  | "llm_gateway"
  | "graph_store"
  | "worker";

/** Enabled capability modes — mirror of `packages/core.errors.ServiceMode`. */
export type ServiceMode = "search" | "ask" | "ingest" | "graph";

/** Criticality of one dependency in one enabled mode — mirror of
 * `packages/core.errors.DependencyRequirement`. `required` ⇒ the mode fails
 * closed (no silent empty results); `optional` ⇒ the mode degrades and
 * discloses; `unused` ⇒ never probe it for that mode. */
export type DependencyRequirement = "required" | "optional" | "unused";
