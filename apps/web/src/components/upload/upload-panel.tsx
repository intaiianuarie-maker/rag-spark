"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useWorkspaces } from "@/components/workspace/workspace-provider";
import { UploadZone } from "./upload-zone";
import { useJobPolling } from "./use-job-polling";

export type UploadPanelProps = {
  collectionId?: string | null;
  onJobTerminal?: (jobId: string, documentId: string) => void;
};

export function UploadPanel({ onJobTerminal }: UploadPanelProps) {
  const { activeId } = useWorkspaces();
  const activeIdRef = useRef(activeId);
  useEffect(() => { activeIdRef.current = activeId; }, [activeId]);
  const [file, setFile] = useState<File | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [documentId, setDocumentId] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const polling = useJobPolling(activeId, jobId, {
    onTerminal: (s) => {
      if (s.status === "ready" && documentId && jobId) {
        onJobTerminal?.(jobId, documentId);
      }
    },
  });

  const handleFiles = useCallback(
    async (files: File[]) => {
      if (files.length === 0) return;
      const wsAtStart = activeIdRef.current;
      if (!wsAtStart) return;
      const f = files[0];
      setFile(f);
      setUploading(true);
      setUploadError(null);
      setJobId(null);
      setDocumentId(null);
      try {
        const { uploadDocument } = await import("@/app/knowledge/contracts");
        const accepted = await uploadDocument(wsAtStart, f);
        // Scope fence: discard if workspace changed mid-flight.
        if (activeIdRef.current !== wsAtStart) return;
        setJobId(accepted.job_id);
        setDocumentId(accepted.document_id);
      } catch (err) {
        if (activeIdRef.current !== wsAtStart) return;
        const msg = err instanceof Error ? err.message : "upload failed";
        setUploadError(msg);
      } finally {
        if (activeIdRef.current === wsAtStart) setUploading(false);
      }
    },
    [],
  );

  const handleRetry = useCallback(() => {
    if (!file) return;
    setUploadError(null);
    // Re-queue per backend semantics: re-upload the same file (backend
    // re-parses the version when the previous attempt failed).
    void handleFiles([file]);
  }, [file, handleFiles]);

  const progress = jobId ? polling.progress : uploading ? 5 : 0;
  const stage = jobId ? polling.stage : uploading ? "uploading" : null;
  const error = uploadError ?? polling.error;

  return (
    <section aria-label="Upload" className="space-y-3">
      <UploadZone onFiles={handleFiles} />
      {file && (
        <div className="rounded-lg border border-neutral-200 p-3 text-sm dark:border-neutral-700">
          <div className="flex items-center justify-between">
            <span className="truncate font-medium">{file.name}</span>
            {jobId && !polling.terminal && !polling.cancelled && (
              <button
                type="button"
                onClick={polling.cancel}
                className="text-xs text-red-600 hover:underline"
                aria-label="Cancel polling"
              >
                Cancel
              </button>
            )}
          </div>
          <div className="mt-2 h-2 w-full overflow-hidden rounded bg-neutral-200 dark:bg-neutral-800">
            <div
              className="h-full bg-blue-500 transition-all"
              style={{ width: `${progress}%` }}
              role="progressbar"
              aria-valuenow={progress}
              aria-valuemin={0}
              aria-valuemax={100}
            />
          </div>
          <p className="mt-1 text-xs text-neutral-500">
            {stage ?? "waiting"} · {progress}%
          </p>
          {error && (
            <div role="alert" className="mt-2 text-xs text-red-600">
              {error}
              {polling.terminal && polling.stage === "failed" && (
                <button
                  type="button"
                  onClick={handleRetry}
                  className="ml-2 underline"
                  aria-label="Retry ingestion"
                >
                  Retry
                </button>
              )}
            </div>
          )}
          {polling.cancelled && (
            <p className="mt-1 text-xs text-amber-600">Polling cancelled.</p>
          )}
        </div>
      )}
    </section>
  );
}
