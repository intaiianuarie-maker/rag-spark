import { DEFAULT_SEARCH_MODE, EMPTY_SEARCH_FILTERS, type SearchFilters, type SearchMode } from "./contracts";

const SEARCH_MODES = ["keyword", "semantic", "hybrid"] as const;

export type SearchUrlState = {
  query: string;
  mode: SearchMode;
  filters: SearchFilters;
};

const FACETS: readonly (keyof SearchFilters)[] = ["type", "source", "date", "collection", "language"];

function isSearchMode(v: string | null): v is SearchMode {
  return v !== null && (SEARCH_MODES as readonly string[]).includes(v);
}

function trimOrNull(v: string | null): string | null {
  if (v == null) return null;
  const t = v.trim();
  return t.length === 0 ? null : t;
}

function toParams(input: URLSearchParams | string | Record<string, unknown>): URLSearchParams {
  if (input instanceof URLSearchParams) return new URLSearchParams(input);
  if (typeof input === "string") {
    // Accept both "?q=..." and "q=..."
    const s = input.startsWith("?") ? input.slice(1) : input;
    return new URLSearchParams(s);
  }
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(input ?? {})) {
    if (v == null) continue;
    if (Array.isArray(v)) {
      for (const item of v) {
        if (item != null) sp.append(k, String(item));
      }
    } else {
      sp.set(k, String(v));
    }
  }
  return sp;
}

export function parseSearchState(input: URLSearchParams | string | Record<string, unknown>): SearchUrlState {
  const sp = toParams(input);
  const query = trimOrNull(sp.get("q")) ?? "";
  const modeRaw = sp.get("mode");
  const mode: SearchMode = isSearchMode(modeRaw) ? modeRaw : DEFAULT_SEARCH_MODE;
  const filters: SearchFilters = { ...EMPTY_SEARCH_FILTERS };
  for (const key of FACETS) {
    const v = trimOrNull(sp.get(key));
    if (v !== null) filters[key] = v;
  }
  return { query, mode, filters };
}

export function searchStateToParams(state: Partial<SearchUrlState> | null | undefined): URLSearchParams {
  const sp = new URLSearchParams();
  const query = trimOrNull((state?.query ?? null) as string | null);
  if (query !== null) sp.set("q", query);
  const mode = state?.mode ?? DEFAULT_SEARCH_MODE;
  if (mode !== DEFAULT_SEARCH_MODE && isSearchMode(mode)) sp.set("mode", mode);
  const filters = state?.filters ?? EMPTY_SEARCH_FILTERS;
  for (const key of FACETS) {
    const v = trimOrNull(filters[key]);
    if (v !== null) sp.set(key, v);
  }
  return sp;
}

export function searchStateToQueryString(state: Partial<SearchUrlState> | null | undefined): string {
  return searchStateToParams(state).toString();
}
