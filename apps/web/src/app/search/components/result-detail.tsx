"use client";

import type { SearchResultItem } from "../contracts";

/**
 * Source-inspection panel: opens the exact passage for a selected result
 * (Kotaemon-style). Mirrors the Ask screen's SourcePanel so the interaction
 * feels consistent across products.
 */
export function ResultDetail({
  item,
  onClose,
}: {
  item: SearchResultItem | null;
  onClose: () => void;
}) {
  if (!item) return null;
  const meta = item.metadata as Record<string, unknown>;
  const filename = typeof meta.filename === "string" ? meta.filename : null;

  return (
    <aside className="w-full md:w-96 md:shrink-0 md:border-l md:border-neutral-200 md:pl-6 md:dark:border-neutral-800">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-neutral-800 dark:text-neutral-200">
          Source
        </h3>
        <button
          type="button"
          onClick={onClose}
          className="text-xs text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300"
          aria-label="Close source panel"
        >
          Close
        </button>
      </div>
      <div className="space-y-3 text-sm text-neutral-700 dark:text-neutral-300">
        <div>
          <span className="text-xs uppercase tracking-wider text-neutral-400">
            Document
          </span>
          <p className="font-medium">
            {filename || item.document_id || item.chunk_id}
          </p>
        </div>
        <div className="flex gap-4">
          <div>
            <span className="text-xs uppercase tracking-wider text-neutral-400">
              Page
            </span>
            <p className="font-medium">{item.page ?? "—"}</p>
          </div>
          <div>
            <span className="text-xs uppercase tracking-wider text-neutral-400">
              Section
            </span>
            <p className="font-medium">{item.section ?? "—"}</p>
          </div>
        </div>
        <div>
          <span className="text-xs uppercase tracking-wider text-neutral-400">
            Method
          </span>
          <p className="font-medium">{item.method}</p>
        </div>
        <div>
          <span className="text-xs uppercase tracking-wider text-neutral-400">
            Score
          </span>
          <p className="font-mono text-xs">{item.score.toFixed(4)}</p>
        </div>
        <div>
          <span className="text-xs uppercase tracking-wider text-neutral-400">
            Passage
          </span>
          <p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed">
            {item.content}
          </p>
        </div>
      </div>
    </aside>
  );
}
