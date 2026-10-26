"use client";

import { auth } from "@/lib/auth-client";

export type SourceKind = "local_upload" | "web" | "s3" | "git";

export type SourceSettings = Record<string, unknown>;

export type Source = {
  connector_id: string;
  kind: SourceKind;
  settings: SourceSettings;
  credential_ref: string | null;
  last_attempt_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
};

export type SourceStatus = {
  connector_id: string;
  kind: string;
  last_attempt_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  last_run_id?: string | null;
  last_finished_at?: string | null;
  state?: "idle" | "running" | "succeeded" | "failed";
  items_seen: number;
  items_created: number;
  items_updated: number;
  items_unchanged: number;
  items_failed: number;
  items_deleted: number;
  cursor: string | null;
};

export type SyncTriggerResult = {
  job_id: string;
  workspace_id: string;
  connector_id: string | null;
  acked: boolean;
};

export async function listSources(workspaceId: string): Promise<Source[]> {
  return auth.authFetch<Source[]>(`/v1/workspaces/${workspaceId}/sources`, {});
}

export async function createSource(
  workspaceId: string,
  body: { connector_id: string; kind: SourceKind; settings: SourceSettings; credential_ref?: string | null },
): Promise<Source> {
  return auth.authFetch<Source>(`/v1/workspaces/${workspaceId}/sources`, {
    method: "POST",
    body,
  });
}

export async function getSource(workspaceId: string, id: string): Promise<Source> {
  return auth.authFetch<Source>(`/v1/workspaces/${workspaceId}/sources/${id}`, {});
}

export async function updateSource(
  workspaceId: string,
  id: string,
  body: { settings?: SourceSettings; credential_ref?: string | null },
): Promise<Source> {
  return auth.authFetch<Source>(`/v1/workspaces/${workspaceId}/sources/${id}`, {
    method: "PATCH",
    body,
  });
}

export async function deleteSource(workspaceId: string, id: string): Promise<void> {
  await auth.authFetch(`/v1/workspaces/${workspaceId}/sources/${id}`, { method: "DELETE" });
}

export async function triggerSourceSync(workspaceId: string, id: string): Promise<SyncTriggerResult> {
  return auth.authFetch<SyncTriggerResult>(`/v1/workspaces/${workspaceId}/sources/${id}/sync`, {
    method: "POST",
  });
}

export async function getSourceStatus(workspaceId: string, id: string): Promise<SourceStatus> {
  return auth.authFetch<SourceStatus>(`/v1/workspaces/${workspaceId}/sources/${id}/status`, {});
}
