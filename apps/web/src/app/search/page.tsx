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
import { parseSearchState, searchStateToParams } from "./url-state";
import {
  createInitialUrlGate,
  markInitialUrlSearchRan,
  shouldRunInitialUrlSearch,
} from "./initial-url-search";

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
  const initial = typeof window !== "undefined" ? parseSearchState(window.location.search) : null;
  const [query, setQuery] = useState(initial?.query ?? "");
  const [mode, setMode] = useState<SearchMode>(initial?.mode ?? DEFAULT_SEARCH_MODE);
  const [filters, setFilters] = useState<SearchFilters>(initial?.filters ?? EMPTY_SEARCH_FILTERS);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Scoped<string> | null>(null);
  const [response, setResponse] = useState<Scoped<SearchResponse> | null>(null);
  const [selected, setSelected] = useState<Scoped<SearchResultItem> | null>(null);
  const runIdRef = useRef(0);
  const lastRunRef = useRef<{ workspaceId: string; runId: number } | null>(null);
  const activeId = active?.id;
  const scopeId = activeId ?? "";
  const [lastActiveId, setLastActiveId] = useState(activeId);
  /**
   * The initial URL-derived search fires exactly once, the moment the active
   * workspace becomes available. Idempotent on active change (M5-001b REV-1):
   * a workspace switch must NOT re-derive a search from the URL — the URL
   * already holds the user's last query and re-running it on every switch
   * would fire duplicate requests. The gate object is created fresh per mount
   * (unmount/remount = a real reload).
   */
  const initialUrlGateRef = useRef(createInitialUrlGate());

  useEffect(() => {
    // Invalidate work started for a workspace that is no longer active, so a
    // delayed result/error can never land under the new workspace.
    //
    // Only OLD-workspace work is invalidated: a run started for the
    // workspace that is ALREADY active — e.g. a request submitted during
    // this workspace's own commit window — belongs to the workspace the
    // caller is looking at and must not be superseded here. (A request
    // started for the old workspace is superseded anyway by the newer run
    // id, so no data of the old workspace is published either way.)
    const lastRun = lastRunRef.current;
    if (lastRun && lastRun.workspaceId !== (activeId ?? "")) {
      runIdRef.current += 1;
    }
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
      // Remember which workspace this run belongs to: the workspace-change
      // invalidation must supersede old-workspace work only, never a run
      // started for the workspace that is already active.
      lastRunRef.current = { workspaceId, runId: thisRun };
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

  // Sync filter/mode/query state to the URL. We use replaceState (not
  // pushState) so filter tweaks do not spam browser history depth; back/
  // forward navigation is still handled via the popstate listener below.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = searchStateToParams({ query, mode, filters });
    const qs = params.toString();
    const url = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
    window.history.replaceState(null, "", url);
  }, [query, mode, filters]);

  // Restore state from the URL on back/forward navigation and re-run search.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onPopState = () => {
      const next = parseSearchState(window.location.search);
      setQuery(next.query);
      setMode(next.mode);
      setFilters(next.filters);
      if (next.query.trim().length > 0) {
        void runSearch(next.query, next.mode, next.filters);
      }
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [runSearch]);

  /**
   * M5-001b REV-1/REV-2 Finding: the mount-only effect used to call runSearch
   * while WorkspaceProvider still had active=null, so runSearch returned
   * immediately and the effect never re-ran when the workspace finally
   * resolved — a reload or pasted URL with q+filters silently did nothing.
   *
   * Now depends on workspace readiness through the one-shot gate in
   * `initial-url-search.ts`: the initial URL-derived search runs exactly once,
   * the moment the active workspace becomes available. The gate is extracted
   * (not just re-read from source) so the delayed-initialization interleaving
   * is pinned by a real test — see `initial-url-search.test.ts`.
   */
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!shouldRunInitialUrlSearch(initialUrlGateRef.current, activeId)) return;
    markInitialUrlSearchRan(initialUrlGateRef.current);
    const initial = parseSearchState(window.location.search);
    if (initial.query.trim().length > 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      void runSearch(initial.query, initial.mode, initial.filters);
    }
  }, [activeId, runSearch]);

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
