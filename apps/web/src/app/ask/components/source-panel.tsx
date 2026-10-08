"use client";

import type { Citation } from "../contracts";

export function SourcePanel({
  citation,
  onClose,
}: {
  citation: Citation | null;
  onClose: () => void;
}) {
  if (!citation) return null;
  return (
    <aside className="w-full md:w-80 md:shrink-0 md:border-l md:border-neutral-200 md:pl-6 md:dark:border-neutral-800">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-neutral-800 dark:text-neutral-200">Source</h3>
        <button
          type="button"
          onClick={onClose}
          className="text-xs text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300"
          aria-label="Close source panel"
        >
          Close
        </button>
      </div>
      <div className="space-y-2 text-sm text-neutral-700 dark:text-neutral-300">
        <div>
          <span className="text-xs uppercase tracking-wider text-neutral-400">Document</span>
          <p className="font-medium">{citation.filename || citation.document_id || citation.chunk_id}</p>
        </div>
        <div className="flex gap-4">
          <div>
            <span className="text-xs uppercase tracking-wider text-neutral-400">Page</span>
            <p className="font-medium">{citation.page ?? "—"}</p>
          </div>
          <div>
            <span className="text-xs uppercase tracking-wider text-neutral-400">Section</span>
            <p className="font-medium">{citation.section ?? "—"}</p>
          </div>
        </div>
        {citation.span && (
          <div>
            <span className="text-xs uppercase tracking-wider text-neutral-400">Span</span>
            <p className="font-mono text-xs">({citation.span[0]}, {citation.span[1]})</p>
          </div>
        )}
      </div>
    </aside>
  );
}
