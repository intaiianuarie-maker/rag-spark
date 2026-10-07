"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { useWorkspaces } from "@/components/workspace/workspace-provider";
import { Empty } from "../../collections-list";
import { JobTimeline, StatusChip } from "../../status-chip";
import {
  READS_UNAVAILABLE,
  getDocumentDetail,
  listDocumentJobs,
  type DocumentDetail,
  type DocumentVersionSummary,
  type JobSummary,
} from "../../contracts";

/** Document detail: metadata, versions, chunk count, job timeline. */
export function DocumentDetailView({ documentId }: { documentId: string }) {
  const { activeId } = useWorkspaces();
  const [document, setDocument] = useState<DocumentDetail | null>(null);
  const [jobs, setJobs] = useState<JobSummary[] | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "unavailable" | "error">("loading");
  const inFlight = useRef(false);

  useEffect(() => {
    if (inFlight.current || activeId === null) return;
    inFlight.current = true;
    setState("loading");
    getDocumentDetail(activeId, documentId)
      .then(async (result: DocumentDetail | typeof READS_UNAVAILABLE) => {
        if (result === READS_UNAVAILABLE) {
          setDocument(null);
          setState("unavailable");
          return;
        }
        setDocument(result);
        const jobResult = await listDocumentJobs(activeId, documentId);
        setJobs(jobResult === READS_UNAVAILABLE ? null : jobResult);
        setState("ready");
      })
      .catch(() => setState("error"))
      .finally(() => {
        inFlight.current = false;
      });
  }, [activeId, documentId]);

  return (
    <section aria-label="Document detail">
      <Link
        href="/knowledge"
        className="inline-flex items-center gap-1.5 text-xs text-neutral-600 hover:opacity-80 dark:text-neutral-400"
      >
        <ArrowLeft className="size-3.5" aria-hidden="true" /> Collections
      </Link>
      <h1 className="mt-2 text-xl font-semibold tracking-tight">
        {document?.title ?? document?.filename ?? "Document"}
      </h1>

      <div className="mt-6 space-y-6">
        {state === "loading" && (
          <div aria-busy="true" className="space-y-2">
            <div className="h-16 animate-pulse rounded-xl bg-neutral-200 dark:bg-neutral-800" />
            <div className="h-8 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
          </div>
        )}

        {state === "unavailable" && (
          <Empty
            title="Document detail arrives with the read API"
            text="The document detail + job timeline endpoints (TASK-M2-003) are not live yet."
          />
        )}

        {state === "error" && (
          <div
            role="alert"
            className="rounded-xl border border-red-600/30 bg-red-50 p-4 text-sm text-red-900 dark:border-red-400/30 dark:bg-red-950/40 dark:text-red-200"
          >
            Could not load this document. Refresh to retry.
          </div>
        )}

        {state === "ready" && document !== null && (
          <>
            <dl className="grid gap-x-6 gap-y-2 rounded-xl border border-neutral-200 bg-white p-4 text-sm sm:grid-cols-2 dark:border-neutral-800 dark:bg-neutral-900">
              <Field label="Filename" value={document.filename} />
              <Field label="MIME type" value={document.mime_type} />
              <Field
                label="Size"
                value={
                  document.size_bytes != null
                    ? `${(document.size_bytes / 1024).toFixed(1)} KiB`
                    : null
                }
              />
              <Field
                label="Status"
                valueNode={<StatusChip state={document.status} />}
              />
              <Field
                label="Current version"
                value={
                  document.current_version?.version != null
                    ? `v${document.current_version.version}`
                    : null
                }
              />
              <Field label="Chunks" value={document.chunk_count != null ? String(document.chunk_count) : null} />
            </dl>

            <div>
              <h2 className="mb-2 text-sm font-semibold">Versions</h2>
              {(document.versions ?? []).length === 0 ? (
                <p className="text-xs text-neutral-500 dark:text-neutral-400">
                  No version history yet.
                </p>
              ) : (
                <ul className="space-y-1 text-sm">
                  {document.versions!.map((version: DocumentVersionSummary, index: number) => (
                    <li key={version.id ?? index} className="flex items-center gap-2">
                      <span className="font-mono text-xs">
                        v{version.version ?? index + 1}
                      </span>
                      {version.created_at ? (
                        <span className="text-xs text-neutral-500 dark:text-neutral-400">
                          {new Date(version.created_at).toLocaleString()}
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div>
              <h2 className="mb-2 text-sm font-semibold">Ingestion jobs</h2>
              {jobs === null ? (
                <p className="text-xs text-neutral-500 dark:text-neutral-400">
                  Job timeline arrives with the read API (TASK-M2-003).
                </p>
              ) : (
                <JobTimeline jobs={jobs} />
              )}
            </div>
          </>
        )}
      </div>
    </section>
  );
}

function Field({
  label,
  value,
  valueNode,
}: {
  label: string;
  value?: string | null;
  valueNode?: React.ReactNode;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-neutral-500 dark:text-neutral-400">{label}</dt>
      <dd className="text-right">{valueNode ?? (value ?? "—")}</dd>
    </div>
  );
}