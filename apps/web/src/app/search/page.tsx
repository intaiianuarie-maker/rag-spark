"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Search as SearchIcon } from "lucide-react";

import { useWorkspaces } from "@/components/workspace/workspace-provider";
import {
  DEFAULT_SEARCH_MODE,
  EMPTY_SEARCH_FILTERS,
  buildSearchBody,
  isValidDateFilter,
  searchDocuments,
  type SearchFilters,
  type SearchMode,
  type SearchResponse,
  type SearchResultItem,
} from "./contracts";
import { ModeToggle } from "./components/mode-toggle";
import { FilterBar } from "./components/filter-bar";
import { ResultList } from "./components/result-list";
import { ResultDetail } from "./components/result-detail";

/**
 * A published value together with the workspace it belongs to.
 *
 * Publication is workspace-scoped on purpose: a response/error produced for
 * workspace A can only ever be *shown* under workspace A. The identity is
 * captured when the request is made, so the value is not re-stamped with
 * whatever workspace happens to be active when it lands — that is what closes
 * the commit window (a stale A resolution landing between B's render and B's
 * passive effect can no longer paint under B, in any commit ordering).
 */
type Scoped<T> = { workspaceId: string; value: T };

export default function SearchPage() {
  const { active } = useWorkspaces();
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>(DEFAULT_SEARCH_MODE);
  const [filters, setFilters] = useState<SearchFilters>(EMPTY_SEARCH_FILTERS);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Scoped<string> | null>(null);
  const [response, setResponse] = useState<Scoped<SearchResponse> | null>(null);
  const [selected, setSelected] = useState<Scoped<SearchResultItem> | null>(null);
  const runIdRef = useRef(0);
  const activeId = active?.id;
  const scopeId = activeId ?? "";
  const [lastActiveId, setLastActiveId] = useState(activeId);

  useEffect(() => {
    // Invalidate any in-flight request from the previous workspace so a
    // delayed result/error can never land under the new workspace.
    runIdRef.current += 1;
  }, [activeId]);

  // Reset the result surface when the active workspace changes. Adjusted
  // during render (React's "adjust state when a prop changes" pattern)
  // instead of from an effect: an effect that resets state synchronously
  // cascades an extra render pass on every workspace switch.
  if (lastActiveId !== activeId) {
    setLastActiveId(activeId);
    setResponse(null);
    setSelected(null);
    setError(null);
    setLoading(false);
  }

  const dateError = !isValidDateFilter(filters.date);

  // Workspace-scoped visibility: a value published for another workspace is
  // never shown under this one. This is the identity fence — it holds in ANY
  // commit ordering, including the window between the render that switches
  // the workspace and the passive effect that invalidates the in-flight run.
  const visibleError = error && error.workspaceId === scopeId ? error.value : null;
  const visibleResponse =
    response && response.workspaceId === scopeId ? response.value : null;
  const visibleSelected =
    selected && selected.workspaceId === scopeId ? selected.value : null;

  const runSearch = useCallback(
    async (q: string, m: SearchMode, f: SearchFilters) => {
      if (!active?.id) return;
      const workspaceId = active.id;
      const thisRun = ++runIdRef.current;
      if (q.trim().length === 0) {
        if (runIdRef.current === thisRun) {
          setError({ workspaceId, value: "Enter a query to search." });
        }
        return;
      }
      if (runIdRef.current !== thisRun) return;
      setLoading(true);
      setError(null);
      setSelected(null);
      try {
        const body = buildSearchBody(q, m, f);
        const result = await searchDocuments(workspaceId, body);
        if (runIdRef.current !== thisRun) return;
        // Published with the identity of the workspace the request was made
        // for — NOT with the workspace that happens to be active when it
        // lands. A late A resolution can therefore never paint under B.
        setResponse({ workspaceId, value: result });
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Search failed.";
        if (runIdRef.current !== thisRun) return;
        setError({ workspaceId, value: msg });
        setResponse(null);
      } finally {
        if (runIdRef.current === thisRun) {
          setLoading(false);
        }
      }
    },
    [active],
  );

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    void runSearch(query, mode, filters);
  };

  return (
    <main className="flex min-h-[calc(100vh-160px)] flex-col md:flex-row">
      <div className="flex-1 flex flex-col">
        <div className="flex items-center justify-between px-6 py-3 md:px-8 md:py-4">
          <h1 className="text-lg font-semibold text-neutral-900 dark:text-neutral-100">
            Search
          </h1>
          <ModeToggle
            value={mode}
            onChange={(m) => {
              setMode(m);
              // Re-run if there's already a query, so mode changes are live.
              if (query.trim().length > 0) {
                void runSearch(query, m, filters);
              }
            }}
          />
        </div>

        <form onSubmit={handleSubmit} className="border-b border-neutral-200 px-6 py-3 md:px-8 dark:border-neutral-800">
          <div className="flex gap-2">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search documents and passages…"
              aria-label="Search query"
              className="flex-1 rounded-md border border-neutral-200 bg-white px-3 py-2 text-sm text-neutral-800 placeholder:text-neutral-400 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-100"
            />
            <button
              type="submit"
              disabled={loading || !active?.id}
              className="inline-flex items-center gap-2 rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white hover:bg-neutral-800 disabled:opacity-50 dark:bg-white dark:text-neutral-900 dark:hover:bg-neutral-200"
            >
              <SearchIcon className="h-4 w-4" />
              {loading ? "Searching…" : "Search"}
            </button>
          </div>
        </form>

        <FilterBar filters={filters} onChange={setFilters} dateError={dateError} />

        <div className="flex-1 overflow-auto">
          {loading && (
            <div className="px-6 py-16 text-center text-sm text-neutral-500 md:px-8 dark:text-neutral-400">
              Searching…
            </div>
          )}
          {!loading && visibleError && (
            <div className="px-6 py-16 text-center text-sm text-red-500 md:px-8">
              {visibleError}
            </div>
          )}
          {!loading && !visibleError && !visibleResponse && (
            <div className="px-6 py-16 text-center text-sm text-neutral-500 md:px-8 dark:text-neutral-400">
              {active?.id
                ? "Enter a query to begin searching."
                : "Select a workspace to search."}
            </div>
          )}
          {!loading && !visibleError && visibleResponse && (
            <ResultList
              results={visibleResponse.results}
              onOpenSource={(item) => setSelected({ workspaceId: scopeId, value: item })}
            />
          )}
        </div>
      </div>

      <ResultDetail item={visibleSelected} onClose={() => setSelected(null)} />
    </main>
  );
}
