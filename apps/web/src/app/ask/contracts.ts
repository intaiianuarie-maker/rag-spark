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

export type AnswerPayload = {
  answer: string;
  claims: Claim[];
  citations: Citation[];
};

export async function askQuestion(
  workspaceId: string,
  query: string,
  model?: string,
): Promise<AnswerPayload> {
  return await auth.authFetch<AnswerPayload>(
    `/v1/workspaces/${workspaceId}/ask`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, ...(model ? { model } : {}) }),
    },
  );
}
