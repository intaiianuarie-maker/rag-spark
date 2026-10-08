"use client";

import type { SearchResultItem } from "../contracts";

const METHOD_LABELS: Record<string, string> = {
  fts: "Keyword",
  vector: "Semantic",
  rrf: "Fused",
  keyword: "Keyword",
  semantic: "Semantic",
  hybrid: "Fused",
};

function methodLabel(method: string): string {
  return METHOD_LABELS[method] ?? method;
}

function docLabel(item: SearchResultItem): string {
  const meta = item.metadata as Record<string, unknown>;
  const filename = typeof meta.filename === "string" ? meta.filename : null;
  return (
    filename ||
    item.document_id ||
    item.chunk_id ||
    "Unknown document"
  );
}

/**
 * Ranked result list. Each row shows the document, method badge, score, and
 * page/section; clicking a row opens the source panel (Kotaemon-style). Score
 * fusion is backend-owned, so we render `item.score` verbatim and never
 * recompute ranking in the UI.
 */
export function ResultList({
  results,
  onOpenSource,
}: {
  results: SearchResultItem[];
  onOpenSource: (item: SearchResultItem) => void;
}) {
  if (results.length === 0) {
    return (
      <div className="px-6 py-16 text-center text-sm text-neutral-500 md:px-8 dark:text-neutral-400">
        No results. Try a broader query or fewer filters.
      </div>
    );
  }

  return (
    <ul className="divide-y divide-neutral-100 dark:divide-neutral-800">
      {results.map((item, idx) => (
        <li key={item.chunk_id ?? idx}>
          <button
            type="button"
            onClick={() => onOpenSource(item)}
            className="w-full px-6 py-4 text-left transition-colors hover:bg-neutral-50 md:px-8 dark:hover:bg-neutral-900/50"
          >
            <div className="mb-1 flex items-center gap-2">
              <span className="text-xs font-medium text-neutral-500 dark:text-neutral-400">
                {docLabel(item)}
              </span>
              <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-[10px] font-medium text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
                {methodLabel(item.method)}
              </span>
              <span className="ml-auto font-mono text-[10px] text-neutral-400">
                {item.score.toFixed(3)}
              </span>
            </div>
            <p className="line-clamp-3 text-sm text-neutral-700 dark:text-neutral-300">
              {item.content}
            </p>
            {(item.page != null || item.section) && (
              <div className="mt-1 flex gap-3 text-[11px] text-neutral-400">
                {item.page != null && <span>p.{item.page}</span>}
                {item.section && <span className="truncate">{item.section}</span>}
              </div>
            )}
          </button>
        </li>
      ))}
    </ul>
  );
}
