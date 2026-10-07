"use client";

/**
 * Knowledge UI contracts + typed client (TASK-M2-002a).
 *
 * Contract status: the read endpoints below are NOT live yet — M2-001c shipped
 * upload + job-status + delete only. Proposed shape sent to boss 2026-10-07
 * (same parallel-stub pattern as the sanctioned M1-001f). Until the backend
 * read ticket lands, 404/405 map to `*_UNAVAILABLE` sentinels and the pages
 * render explicit "not available yet" states instead of faking data.
 */

import { auth, AuthApiError } from "@/lib/auth-client";

export type JobState =
  | "queued"
  | "parsing"
  | "chunking"
  | "embedding"
  | "ready"
  | "failed";

export type JobSummary = {
  id: string;
  state: JobState;
  attempts?: number | null;
  error?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type CollectionPublic = {
  id: string;
  name: string;
  description?: string | null;
  document_count?: number | null;
  created_at?: string | null;
};

export type DocumentSummary = {
  id: string;
  collection_id?: string | null;
  title?: string | null;
  filename?: string | null;
  status?: string | null;
  version_count?: number | null;
  chunk_count?: number | null;
  size_bytes?: number | null;
  mime_type?: string | null;
  updated_at?: string | null;
};

export type DocumentVersionSummary = {
  id?: string | null;
  version?: number | null;
  checksum?: string | null;
  created_at?: string | null;
};

export type DocumentDetail = DocumentSummary & {
  created_at?: string | null;
  current_version?: DocumentVersionSummary | null;
  versions?: DocumentVersionSummary[];
  jobs?: JobSummary[];
};

export const READS_UNAVAILABLE = "readsUnavailable" as const;

type Sentinel = typeof READS_UNAVAILABLE;

function isUnavailable(error: unknown): boolean {
  return error instanceof AuthApiError && (error.status === 404 || error.status === 405);
}

/** GET /v1/workspaces/{ws}/collections */
export async function listCollections(
  workspaceId: string,
): Promise<CollectionPublic[] | Sentinel> {
  try {
    return await auth.authFetch<CollectionPublic[]>(
      `/v1/workspaces/${workspaceId}/collections`,
      {},
    );
  } catch (error) {
    if (isUnavailable(error)) return READS_UNAVAILABLE;
    throw error;
  }
}

/** GET /v1/workspaces/{ws}/collections/{collection}/documents */
export async function listCollectionDocuments(
  workspaceId: string,
  collectionId: string,
): Promise<DocumentSummary[] | Sentinel> {
  try {
    return await auth.authFetch<DocumentSummary[]>(
      `/v1/workspaces/${workspaceId}/collections/${collectionId}/documents`,
      {},
    );
  } catch (error) {
    if (isUnavailable(error)) return READS_UNAVAILABLE;
    throw error;
  }
}

/** GET /v1/workspaces/{ws}/documents/{document} */
export async function getDocumentDetail(
  workspaceId: string,
  documentId: string,
): Promise<DocumentDetail | Sentinel> {
  try {
    return await auth.authFetch<DocumentDetail>(
      `/v1/workspaces/${workspaceId}/documents/${documentId}`,
      {},
    );
  } catch (error) {
    if (isUnavailable(error)) return READS_UNAVAILABLE;
    throw error;
  }
}

/** GET /v1/workspaces/{ws}/documents/{document}/jobs */
export async function listDocumentJobs(
  workspaceId: string,
  documentId: string,
): Promise<JobSummary[] | Sentinel> {
  try {
    return await auth.authFetch<JobSummary[]>(
      `/v1/workspaces/${workspaceId}/documents/${documentId}/jobs`,
      {},
    );
  } catch (error) {
    if (isUnavailable(error)) return READS_UNAVAILABLE;
    throw error;
  }
}