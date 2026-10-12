"use client";

/**
 * Search contracts + typed API client (TASK-M5-002).
 *
 * Shapes are the frozen backend contract of TASK-M5-002a
 * (POST /v1/workspaces/{id}/search):
 *   - mode: keyword (lexical-only) | semantic (dense-only) | hybrid (DEFAULT)
 *   - filters: type/source/date/collection/language, all optional
 *   - date is a post-retrieval predicate: "YYYY-MM-DD" exact OR
 *     "YYYY-MM-DD..YYYY-MM-DD" inclusive range.
 *
 * This module is deliberately UI-framework-free and side-effect-free so the
 * pure helpers (`buildSearchBody`, `isValidDateFilter`) are unit-testable in
 * the node vitest environment (see contracts.test.ts).
 */

import { auth } from "@/lib/auth-client";

export type SearchMode = "keyword" | "semantic" | "hybrid";

export const SEARCH_MODES: readonly SearchMode[] = ["keyword", "semantic", "hybrid"];

/** Hybrid RRF fusion is the backend default and the UI's initial selection. */
export const DEFAULT_SEARCH_MODE: SearchMode = "hybrid";

export type SearchFilters = {
  type: string | null;
  source: string | null;
  date: string | null;
  collection: string | null;
  language: string | null;
};

export const EMPTY_SEARCH_FILTERS: SearchFilters = {
  type: null,
  source: null,
  date: null,
  collection: null,
  language: null,
};

export type SearchResultItem = {
  chunk_id: string | null;
  document_id: string | null;
  content: string;
  score: number;
  method: string;
  page: number | null;
  section: string | null;
  workspace_id: string | null;
  scores: Record<string, number>;
  metadata: Record<string, unknown>;
};

export type SearchResponse = {
  results: SearchResultItem[];
  mode: string;
};

const DATE_EXACT = /^\d{4}-\d{2}-\d{2}$/;
const DATE_RANGE = /^\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}$/;

/**
 * Validate a client-side date predicate: empty (unset) is valid; otherwise it
 * must be an exact ISO day or an inclusive `start..end` range. The backend
 * applies the predicate post-retrieval, so the UI guards the shape up front.
 */
export function isValidDateFilter(date: string | null | undefined): boolean {
  if (date == null) return true;
  const trimmed = date.trim();
  if (trimmed.length === 0) return true;
  return DATE_EXACT.test(trimmed) || DATE_RANGE.test(trimmed);
}

function isBlank(value: string | null | undefined): boolean {
  return value == null || value.trim().length === 0;
}

/**
 * Build the request body for POST /search. Empty-string filters are DROPPED
 * (never sent as ""), because the backend treats any non-null value as an
 * active predicate — an empty type would otherwise match nothing and silently
 * over-narrow every query. Mode falls back to the hybrid default.
 */
export function buildSearchBody(
  query: string,
  mode: SearchMode = DEFAULT_SEARCH_MODE,
  filters: SearchFilters = EMPTY_SEARCH_FILTERS,
): { query: string; mode: SearchMode; filters: SearchFilters } {
  const clean: SearchFilters = {
    type: isBlank(filters.type) ? null : filters.type!.trim(),
    source: isBlank(filters.source) ? null : filters.source!.trim(),
    date: isBlank(filters.date) ? null : filters.date!.trim(),
    collection: isBlank(filters.collection) ? null : filters.collection!.trim(),
    language: isBlank(filters.language) ? null : filters.language!.trim(),
  };
  return {
    query,
    mode: mode ?? DEFAULT_SEARCH_MODE,
    filters: clean,
  };
}

export async function searchDocuments(
  workspaceId: string,
  body: { query: string; mode: SearchMode; filters: SearchFilters },
): Promise<SearchResponse> {
  return await auth.authFetch<SearchResponse>(
    `/v1/workspaces/${workspaceId}/search`,
    {
      method: "POST",
      body,
    },
  );
}
