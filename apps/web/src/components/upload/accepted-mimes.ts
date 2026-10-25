// Derived from the backend parser registry (packages/ingestion/adapters/registry.py
// `supported_mime_types()`). Keep in sync; client validation only, backend is
// authoritative. Do not invent mimes not present in the registry.
export const ACCEPTED_MIME_TYPES: readonly string[] = [
  "application/csv",
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
  "application/xhtml+xml",
  "text/csv",
  "text/html",
  "text/markdown",
  "text/plain",
  "text/x-markdown",
];

export const ACCEPTED_MIME_SET: ReadonlySet<string> = new Set(ACCEPTED_MIME_TYPES);

export function isAcceptedMime(mime: string | null | undefined): boolean {
  if (!mime) return false;
  return ACCEPTED_MIME_SET.has(mime.toLowerCase());
}
