"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { auth } from "@/lib/auth-client";
import { isTerminalStage, stageToProgress } from "./stage-progress";
import { mapJobError } from "./polling";

export type JobStatusResponse = {
  job_id: string;
  document_id: string;
  status: string;
  stage: string;
  attempts: number;
  max_attempts: number;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  finished_at: string | null;
};

export type PollingState = {
  status: string | null;
  stage: string | null;
  progress: number;
  error: string | null;
  cancelled: boolean;
  terminal: boolean;
};

export type UseJobPollingOptions = {
  intervalMs?: number;
  onTerminal?: (s: JobStatusResponse) => void;
};

/**
 * Pure stale-response fence predicate. A fetch result is stale (must be
 * discarded) when its token is no longer the latest, or the workspace/job
 * changed mid-flight. Exported for unit testing.
 */
export function isStaleFetch(
  token: number,
  currentToken: number,
  startWorkspace: string | null,
  startJob: string | null,
  currentWorkspace: string | null,
  currentJob: string | null,
): boolean {
  if (token !== currentToken) return true;
  if (startWorkspace !== currentWorkspace) return true;
  if (startJob !== currentJob) return true;
  return false;
}

export function useJobPolling(
  workspaceId: string | null,
  jobId: string | null,
  opts: UseJobPollingOptions = {},
) {
  const intervalMs = opts.intervalMs ?? 2000;
  const [state, setState] = useState<PollingState>({
    status: null,
    stage: null,
    progress: 0,
    error: null,
    cancelled: false,
    terminal: false,
  });

  const cancelledRef = useRef(false);
  const terminalRef = useRef(false);
  const timerRef = useRef<number | null>(null);
  const wsRef = useRef(workspaceId);
  const jobRef = useRef(jobId);
  const fetchOnceRef = useRef<() => void>(() => undefined);
  // Monotonic fetch token: a response is applied only if its token matches
  // the latest issued one AND the workspace/job are unchanged. This is the
  // stale-response fence (no cross-job/cross-workspace leakage).
  const fetchTokenRef = useRef(0);
  const onTerminalRef = useRef(opts.onTerminal);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const cancel = useCallback(() => {
    cancelledRef.current = true;
    clearTimer();
    setState((s) => ({ ...s, cancelled: true }));
  }, [clearTimer]);

  const retry = useCallback(() => {
    // Re-queue a failed job: drop terminal/cancelled flags and restart.
    cancelledRef.current = false;
    terminalRef.current = false;
    fetchTokenRef.current += 1;
    clearTimer();
    setState({ status: null, stage: null, progress: 0, error: null, cancelled: false, terminal: false });
    timerRef.current = window.setTimeout(() => {
      void fetchOnceRef.current();
    }, intervalMs) as unknown as number;
  }, [clearTimer, intervalMs]);

  const fetchOnce = useCallback(async () => {
    const ws = wsRef.current;
    const job = jobRef.current;
    if (!ws || !job || cancelledRef.current || terminalRef.current) return;
    if (typeof document !== "undefined" && document.visibilityState === "hidden") {
      return;
    }
    const token = ++fetchTokenRef.current;
    const startWs = ws;
    const startJob = job;
    try {
      const res = await auth.authFetch<JobStatusResponse>(`/v1/workspaces/${ws}/jobs/${job}`, {});
      // Stale-response fence: discard if cancelled, terminal, token stale,
      // or workspace/job changed mid-flight.
      if (cancelledRef.current || terminalRef.current) return;
      if (isStaleFetch(token, fetchTokenRef.current, startWs, startJob, wsRef.current, jobRef.current)) return;
      const progress = stageToProgress(res.stage);
      const terminal = isTerminalStage(res.stage) || res.status === "ready" || res.status === "failed";
      const error =
        res.error_code
          ? mapJobError(res.error_code, res.error_message ?? undefined)
          : res.status === "failed"
            ? mapJobError(null)
            : null;
      setState((s) => ({ ...s, status: res.status, stage: res.stage, progress, error, terminal }));
      if (terminal) {
        terminalRef.current = true;
        clearTimer();
        onTerminalRef.current?.(res);
        return;
      }
    } catch (err) {
      if (cancelledRef.current || terminalRef.current) return;
      if (isStaleFetch(token, fetchTokenRef.current, startWs, startJob, wsRef.current, jobRef.current)) return;
      const msg = err instanceof Error ? err.message : "network error";
      setState((s) => ({ ...s, error: mapJobError("QUEUE_UNAVAILABLE", msg) }));
    }
    if (!cancelledRef.current && !terminalRef.current && wsRef.current === startWs && jobRef.current === startJob) {
      timerRef.current = window.setTimeout(() => {
        void fetchOnceRef.current();
      }, intervalMs) as unknown as number;
    }
  }, [intervalMs, clearTimer]);

  useEffect(() => {
    fetchOnceRef.current = () => {
      void fetchOnce();
    };
  }, [fetchOnce]);

  useEffect(() => {
    onTerminalRef.current = opts.onTerminal;
  }, [opts.onTerminal]);

  useEffect(() => {
    wsRef.current = workspaceId;
    jobRef.current = jobId;
  }, [workspaceId, jobId]);

  useEffect(() => {
    if (!workspaceId || !jobId) return;
    cancelledRef.current = false;
    terminalRef.current = false;
    fetchTokenRef.current += 1;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setState({ status: null, stage: null, progress: 0, error: null, cancelled: false, terminal: false });
    clearTimer();
    timerRef.current = window.setTimeout(() => {
      void fetchOnce();
    }, intervalMs) as unknown as number;

    const onVisibility = () => {
      if (cancelledRef.current || terminalRef.current) return;
      if (document.visibilityState === "visible") {
        clearTimer();
        void fetchOnce();
      } else {
        clearTimer();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      clearTimer();
      // Invalidate any in-flight response so it cannot land after unmount/scope change.
      fetchTokenRef.current += 1;
      cancelledRef.current = true;
    };
  }, [workspaceId, jobId, intervalMs, fetchOnce, clearTimer]);

  return { ...state, cancel, retry, progress: state.progress };
}
