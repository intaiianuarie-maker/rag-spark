"use client";

import type { Citation } from "../contracts";

export function CitationChip({
  citation,
  onOpen,
}: {
  citation: Citation;
  onOpen: (c: Citation) => void;
}) {
  const label = citation.filename || citation.document_id || citation.chunk_id;
  return (
    <button
      type="button"
      onClick={() => onOpen(citation)}
      className="inline-flex items-center gap-1 rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-medium text-blue-700 ring-1 ring-inset ring-blue-200 hover:bg-blue-100 hover:ring-blue-300 dark:bg-blue-900/20 dark:text-blue-300 dark:ring-blue-700/40"
      title={`${label} · page ${citation.page ?? "?"}`}
    >
      <span className="truncate max-w-[120px]">{label}</span>
      <span className="text-[10px] opacity-60">p.{citation.page ?? "?"}</span>
    </button>
  );
}
