"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useWorkspaces } from "@/components/workspace/workspace-provider";
import { Empty } from "../collections-list";
import { StatusChip } from "../status-chip";
import { UploadPanel } from "@/components/upload/upload-panel";
import { READS_UNAVAILABLE, listCollectionDocuments, type DocumentSummary } from "../contracts";

/** Collection document list with status chips (`/knowledge/[collectionId]`). */
export function CollectionDocuments({ collectionId }: { collectionId: string }) {
  const { activeId, status } = useWorkspaces();
  const [documents, setDocuments] = useState<DocumentSummary[] | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "unavailable" | "error">("loading");
  const inFlight = useRef(false);

  useEffect(() => {
    if (inFlight.current || activeId === null) return;
    inFlight.current = true;
    setState("loading");
    listCollectionDocuments(activeId, collectionId)
      .then((result: DocumentSummary[] | typeof READS_UNAVAILABLE) => {
        if (result === READS_UNAVAILABLE) {
          setDocuments(null);
          setState("unavailable");
        } else {
          setDocuments(result);
          setState("ready");
        }
      })
      .catch(() => setState("error"))
      .finally(() => {
        inFlight.current = false;
      });
  }, [activeId, collectionId]);

  return (
    <section aria-label="Documents">
      <Link
        href="/knowledge"
        className="inline-flex items-center gap-1.5 text-xs text-neutral-600 hover:opacity-80 dark:text-neutral-400"
      >
        <ArrowLeft className="size-3.5" aria-hidden="true" /> Collections
      </Link>
      <h1 className="mt-2 text-xl font-semibold tracking-tight">Documents</h1>

      <div className="mt-4">
        <UploadPanel />
      </div>

      <div className="mt-6">
        {status === "loading" && (
          <div aria-busy="true" className="space-y-2">
            <div className="h-8 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
            <div className="h-8 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
          </div>
        )}

        {status !== "loading" && activeId === null && (
          <Empty title="No workspace selected" text="Switch to a workspace first." />
        )}

        {state === "loading" && (
          <div aria-busy="true" className="space-y-2">
            <div className="h-8 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
            <div className="h-8 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
          </div>
        )}

        {state === "unavailable" && (
          <Empty
            title="Document list arrives with the read API"
            text="The collection document list endpoint (TASK-M2-003) is not live yet."
          />
        )}

        {state === "error" && (
          <div
            role="alert"
            className="rounded-xl border border-red-600/30 bg-red-50 p-4 text-sm text-red-900 dark:border-red-400/30 dark:bg-red-950/40 dark:text-red-200"
          >
            Could not load documents. Refresh to retry.
          </div>
        )}

        {state === "ready" && (documents ?? []).length === 0 && (
          <Empty
            title="No documents in this collection"
            text="Uploaded documents appear here once they finish parsing."
          />
        )}

        {state === "ready" && (documents ?? []).length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Document</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Versions</TableHead>
                <TableHead>Chunks</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {documents!.map((document) => (
                <TableRow key={document.id}>
                  <TableCell className="max-w-72">
                    <Link
                      href={`/knowledge/documents/${document.id}`}
                      className="block truncate font-medium hover:underline"
                    >
                      {document.title ?? document.filename ?? document.id}
                    </Link>
                    {document.filename && document.title ? (
                      <span className="block truncate text-xs text-neutral-500 dark:text-neutral-400">
                        {document.filename}
                      </span>
                    ) : null}
                  </TableCell>
                  <TableCell>
                    <StatusChip state={document.status} />
                  </TableCell>
                  <TableCell className="text-sm">{document.version_count ?? "—"}</TableCell>
                  <TableCell className="text-sm">{document.chunk_count ?? "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </section>
  );
}