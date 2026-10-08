"use client";

import {
  DEFAULT_SEARCH_MODE,
  SEARCH_MODES,
  type SearchMode,
} from "../contracts";

const MODE_LABELS: Record<SearchMode, string> = {
  keyword: "Keyword",
  semantic: "Semantic",
  hybrid: "Hybrid",
};

const MODE_HINTS: Record<SearchMode, string> = {
  keyword: "Lexical (FTS)",
  semantic: "Dense (vectors)",
  hybrid: "Fused (RRF)",
};

/**
 * Keyword / Semantic / Hybrid selector. Hybrid is the default and the initial
 * selection (matches the backend default). Rendered as an accessible radiogroup
 * so the toggle state is unambiguous for assistive tech.
 */
export function ModeToggle({
  value,
  onChange,
}: {
  value: SearchMode;
  onChange: (mode: SearchMode) => void;
}) {
  return (
    <div
      role="radiogroup"
      aria-label="Retrieval mode"
      className="inline-flex rounded-md border border-neutral-200 bg-neutral-50 p-0.5 dark:border-neutral-700 dark:bg-neutral-900"
    >
      {SEARCH_MODES.map((mode) => {
        const selected = mode === value;
        return (
          <button
            key={mode}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onChange(mode)}
            title={MODE_HINTS[mode]}
            className={`rounded px-3 py-1 text-sm font-medium transition-colors ${
              selected
                ? "bg-white text-neutral-900 shadow-sm dark:bg-neutral-700 dark:text-neutral-50"
                : "text-neutral-500 hover:text-neutral-700 dark:text-neutral-400 dark:hover:text-neutral-200"
            }`}
          >
            {MODE_LABELS[mode]}
            {mode === DEFAULT_SEARCH_MODE && (
              <span className="ml-1 text-[10px] font-normal text-neutral-400">·default</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
