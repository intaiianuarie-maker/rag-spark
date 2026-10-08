"use client";

/**
 * Trace panel presentational component (TASK-M8-002). Pure render over the
 * framework-free `trace-panel.ts` helpers so it stays unit-testable.
 */

import { activeLegs, traceBars, TRACE_LABELS, type TraceKey } from "./trace-panel";

export function TracePanel({ scores }: { scores: Record<string, number> | undefined }) {
  const bars = traceBars(scores);
  const legs = activeLegs(scores);

  if (bars.length === 0) {
    return (
      <p className="text-xs text-neutral-400 dark:text-neutral-500">
        No retrieval trace available for this result.
      </p>
    );
  }

  return (
    <div className="space-y-2" data-testid="trace-panel">
      <div className="flex flex-wrap gap-1.5" aria-label="Retrieval legs that fired">
        {legs.map((leg: TraceKey) => (
          <span
            key={leg}
            className="rounded-full bg-neutral-100 px-2 py-0.5 text-[11px] font-medium text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300"
          >
            {TRACE_LABELS[leg]}
          </span>
        ))}
      </div>

      <ul className="space-y-1.5">
        {bars.map(({ key, value, widthPct }) => (
          <li key={key} className="flex items-center gap-2 text-xs">
            <span className="w-24 shrink-0 text-neutral-500 dark:text-neutral-400">
              {TRACE_LABELS[key]}
            </span>
            <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-neutral-100 dark:bg-neutral-800">
              <span
                className="block h-full rounded-full bg-neutral-500 dark:bg-neutral-400"
                style={{ width: `${widthPct}%` }}
              />
            </span>
            <span className="w-16 shrink-0 text-right font-mono text-neutral-600 dark:text-neutral-300">
              {value.toFixed(4)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
