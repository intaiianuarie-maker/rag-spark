"use client";

import type { SearchFilters } from "../contracts";

/**
 * Filter bar for type / source / date / collection / language. All fields are
 * optional and free-form; empty values are dropped by `buildSearchBody` before
 * the request is sent. `dateError` surfaces an invalid date predicate (exact
 * ISO day or inclusive `start..end` range) without blocking the request — the
 * user can still search, but the hint explains why a date filter may not apply.
 */
export function FilterBar({
  filters,
  onChange,
  dateError,
}: {
  filters: SearchFilters;
  onChange: (next: SearchFilters) => void;
  dateError: boolean;
}) {
  const set = (key: keyof SearchFilters) => (value: string) =>
    onChange({ ...filters, [key]: value });

  return (
    <div className="flex flex-wrap items-end gap-3 border-b border-neutral-200 px-6 py-3 md:px-8 dark:border-neutral-800">
      <Field label="Type" placeholder="e.g. pdf">
        <input
          type="text"
          value={filters.type ?? ""}
          onChange={(e) => set("type")(e.target.value)}
          placeholder="e.g. pdf"
          aria-label="Filter by type"
          className={inputClass}
        />
      </Field>
      <Field label="Source" placeholder="e.g. confluence">
        <input
          type="text"
          value={filters.source ?? ""}
          onChange={(e) => set("source")(e.target.value)}
          placeholder="e.g. confluence"
          aria-label="Filter by source"
          className={inputClass}
        />
      </Field>
      <Field
        label="Date"
        hint="YYYY-MM-DD or start..end"
        error={dateError}
      >
        <input
          type="text"
          value={filters.date ?? ""}
          onChange={(e) => set("date")(e.target.value)}
          placeholder="2026-01-01..2026-03-31"
          aria-label="Filter by date"
          aria-invalid={dateError}
          className={`${inputClass} ${
            dateError ? "border-red-400 focus:ring-red-400" : ""
          }`}
        />
      </Field>
      <Field label="Collection" placeholder="e.g. finance">
        <input
          type="text"
          value={filters.collection ?? ""}
          onChange={(e) => set("collection")(e.target.value)}
          placeholder="e.g. finance"
          aria-label="Filter by collection"
          className={inputClass}
        />
      </Field>
      <Field label="Language" placeholder="e.g. en">
        <input
          type="text"
          value={filters.language ?? ""}
          onChange={(e) => set("language")(e.target.value)}
          placeholder="e.g. en"
          aria-label="Filter by language"
          className={inputClass}
        />
      </Field>
    </div>
  );
}

const inputClass =
  "w-full rounded-md border border-neutral-200 bg-white px-2.5 py-1.5 text-sm text-neutral-800 placeholder:text-neutral-400 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-100";

function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: boolean;
  placeholder?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span
        className={`text-[11px] font-medium uppercase tracking-wider ${
          error ? "text-red-500" : "text-neutral-400"
        }`}
      >
        {label}
        {hint && !error && (
          <span className="ml-1 font-normal normal-case tracking-normal text-neutral-400">
            · {hint}
          </span>
        )}
        {error && (
          <span className="ml-1 font-normal normal-case tracking-normal text-red-500">
            · invalid
          </span>
        )}
      </span>
      {children}
    </label>
  );
}
