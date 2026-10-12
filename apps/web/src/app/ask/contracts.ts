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

/* --- TASK-Q0 additive contract (mirrors packages/core/errors.py) ---
 * Purely additive exports; existing payload types above are unchanged.
 */

/** Answer `status` values — mirror of `packages/answer/models.py` AnswerStatus. */
export type AnswerStatus =
  | "answered"
  | "partial"
  | "insufficient_evidence"
  | "clarification_needed"
  | "unavailable";

/** Canonical reason codes the ask endpoint may emit in `reason_code`
 * (mirror of the ANSWER subset of `packages/core.errors.ReasonCode`;
 * locked by tests/contracts/test_error_taxonomy_contract.py).
 * `reason_code` stays `string | null` on the wire for compatibility. */
export type AnswerReasonCode =
  | "PARSE_FALLBACK"
  | "EVIDENCE_MISSING"
  | "NO_CLAIMS"
  | "CITATION_INVALID"
  | "PARTIAL_EVIDENCE";
