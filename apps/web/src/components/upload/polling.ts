export function mapJobError(code: string | null | undefined, fallback?: string): string {
  switch (code) {
    case "UNSUPPORTED_MIME_TYPE":
      return "Unsupported file type.";
    case "EMPTY_DOCUMENT":
      return "Uploaded file is empty.";
    case "DOCUMENT_TOO_LARGE":
      return "File is too large.";
    case "PARSE_ERROR":
      return "Could not parse the document.";
    case "QUEUE_UNAVAILABLE":
      return "Ingestion queue is unavailable; try again shortly.";
    default:
      return fallback && fallback.trim().length > 0 ? fallback : "Ingestion failed.";
  }
}

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

export type PollerCallbacks = {
  fetchStatus: () => Promise<JobStatusResponse>;
  onStatus: (s: JobStatusResponse) => void;
  onError: (message: string) => void;
  onTerminal: (s: JobStatusResponse) => void;
  onVisibilityChange?: () => "visible" | "hidden";
};

export type Poller = {
  start: () => void;
  cancel: () => void;
  pause: () => void;
  resume: () => void;
  isCancelled: () => boolean;
  isTerminal: () => boolean;
};

export function createJobPoller(
  callbacks: PollerCallbacks,
  opts: { intervalMs?: number; setTimeoutFn?: typeof setTimeout; clearTimeoutFn?: typeof clearTimeout } = {},
): Poller {
  const intervalMs = opts.intervalMs ?? 2000;
  const setTimeoutFn = opts.setTimeoutFn ?? setTimeout;
  const clearTimeoutFn = opts.clearTimeoutFn ?? clearTimeout;
  const getVisibility = callbacks.onVisibilityChange ?? (() => "visible");

  let timer: ReturnType<typeof setTimeout> | null = null;
  let cancelled = false;
  let terminal = false;

  function clearTimer() {
    if (timer !== null) {
      clearTimeoutFn(timer);
      timer = null;
    }
  }

  async function tick() {
    if (cancelled || terminal) return;
    if (getVisibility() === "hidden") {
      return;
    }
    try {
      const s = await callbacks.fetchStatus();
      if (cancelled) return;
      callbacks.onStatus(s);
      if (s.stage.toLowerCase() === "ready" || s.status.toLowerCase() === "ready") {
        terminal = true;
        callbacks.onTerminal(s);
        return;
      }
      if (s.stage.toLowerCase() === "failed" || s.status.toLowerCase() === "failed") {
        terminal = true;
        callbacks.onError(mapJobError(s.error_code, s.error_message ?? undefined));
        callbacks.onTerminal(s);
        return;
      }
    } catch (err) {
      if (cancelled) return;
      const msg = err instanceof Error ? err.message : "network error";
      callbacks.onError(mapJobError("QUEUE_UNAVAILABLE", msg));
    }
    if (!cancelled && !terminal && getVisibility() === "visible") {
      timer = setTimeoutFn(() => {
        void tick();
      }, intervalMs) as unknown as ReturnType<typeof setTimeout>;
    }
  }

  function start() {
    cancelled = false;
    terminal = false;
    timer = setTimeoutFn(() => {
      void tick();
    }, intervalMs) as unknown as ReturnType<typeof setTimeout>;
  }

  function cancel() {
    cancelled = true;
    clearTimer();
  }

  function pause() {
    clearTimer();
  }

  function resume() {
    if (cancelled || terminal) return;
    timer = setTimeoutFn(() => {
      void tick();
    }, intervalMs) as unknown as ReturnType<typeof setTimeout>;
  }

  return {
    start,
    cancel,
    pause,
    resume,
    isCancelled: () => cancelled,
    isTerminal: () => terminal,
  };
}
