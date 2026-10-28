/**
 * SSE streaming client for POST /v1/workspaces/{workspace_id}/ask/stream.
 *
 * Consumes the server SSE stream and exposes it as an async iterator of
 * structured events. Uses `eventsource-parser` (already in the lockfile) so
 * the parse logic is battle-tested against the same wire format the frontend
 * expects.
 *
 * SSE wire contract (from TASK-M4-001b):
 *   event: token
 *     data: {"text": "<partial-answer-chunk>"}
 *   event: citations
 *     data: {…full AnswerPayload with verified citations …}
 *   event: error
 *     data: {"error": "<safe-reason>", "code": "<ReasonCode>", "trace_id": "…"}
 *
 * Usage:
 *   const it = streamQuestion(workspaceId, query, model);
 *   for await (const event of it) {
 *     if (event.type === "token") appendText(event.text);
 *     else if (event.type === "citations") finalize(event.answer);
 *     else if (event.type === "error") reportError(event);
 *   }
 */
import { createParser, type EventSourceMessage } from "eventsource-parser";

import type { AnswerPayload } from "./contracts";

// ---------------------------------------------------------------------------
// Public event shape
// ---------------------------------------------------------------------------

/** A single SSE event received from the streaming endpoint. */
export type StreamEvent =
  | { type: "token"; text: string; turnId?: string }
  | { type: "citations"; answer: AnswerPayload; turnId?: string }
  | { type: "error"; code: string; message: string; traceId?: string | null; turnId?: string };

/**
 * The feedback payload shape documented for the Ask screen.
 *
 * There is currently no backend endpoint for feedback (TASK-M5-001a is frontend
 * only). The shape is pinned here so downstream work can hook a POST route
 * against it without re-designing the contract. If a backend endpoint is added
 * later, its handler should accept this exact shape (or a strict superset).
 *
 * Local persistence decision: because there is no backend route, callers may
 * persist feedback locally (e.g. via `browserStorage` keyed by turn id) until
 * a server-side store is available. The shape below supports both modes.
 */
export type FeedbackPayload = {
  /** ID of the turn/message being rated — the turn's index in the transcript, or a stable generated id. */
  turnId: string;
  /** Rating: positive, negative, or neutral/none. */
  rating: "positive" | "negative" | "none";
  /** Optional free-text comment attached to the rating. */
  comment?: string;
  /** ISO-8601 timestamp of when the feedback was given. */
  answeredAt: string;
};

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function parseData(raw: string): unknown {
  if (raw.trim().length === 0) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/**
 * True when a read failure is the intentional result of the caller's
 * AbortSignal (user pressed Stop) — a clean stop, NOT a stream error.
 *
 * undici names the AbortError `DOMException` ("The operation was aborted") or
 * an `Error` ("AbortError"); both are matched here so intentional abort is
 * never surfaced to the consumer as an error event.
 */
function isAbortError(error: unknown): boolean {
  if (error instanceof DOMException) return error.name === "AbortError";
  return error instanceof Error && error.name === "AbortError";
}

/**
 * M5-001a REV-2 Finding 4: when the backend answers with a non-2xx status AND
 * an SSE `event: error` body, the error envelope lives in the frame, not in a
 * JSON object. Parse the body per content-type and surface the frame's
 * code/message/trace_id. Returns null when the body is not an SSE error frame
 * (so the caller falls back to the JSON envelope path).
 */
async function readSseError(
  response: Response,
): Promise<{ code: string; message: string; traceId: string | null } | null> {
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().includes("text/event-stream")) return null;
  if (!response.body) return null;

  let text: string;
  try {
    text = await response.text();
  } catch {
    return null;
  }

  // First `event: error` block, with its `data:` line.
  const match = text.match(/event:\s*error[\s\S]*?data:\s*([^\n]*)/i);
  if (!match) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(match[1]);
  } catch {
    return null;
  }
  if (!isRecord(parsed)) return null;

  const code =
    typeof parsed.code === "string" ? parsed.code : "stream_error";
  const message =
    typeof parsed.error === "string"
      ? parsed.error
      : typeof parsed.message === "string"
        ? parsed.message
        : "Streaming answer failed.";
  const traceId = typeof parsed.trace_id === "string" ? parsed.trace_id : null;
  return { code, message, traceId };
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Open a streaming Ask request and yield structured events.
 *
 * Uses same-origin `fetch` directly (cookies are sent automatically for
 * same-origin requests). The `authFetch` helper from auth-client is not used
 * here because it parses the response body as JSON on every call — incompatible
 * with SSE streaming.
 *
 * The caller is responsible for calling `abort()` on the AbortSignal when the
 * user presses Stop. The stream will close gracefully and the generator will
 * finish (not throw) when the signal is aborted.
 *
 * `turnId` is the caller's stable key for THIS turn (M5-001a REV-2 Finding 2):
 * it lets the page scope its streaming state to the originating turn, so a
 * workspace switch mid-generation can never deliver A's tokens onto B's last
 * assistant message. It is echoed on every event so the page can ignore events
 * that belong to a superseded turn.
 */
export async function* streamQuestion(
  workspaceId: string,
  query: string,
  model?: string,
  signal?: AbortSignal,
  turnId?: string,
): AsyncGenerator<StreamEvent> {
  const headers: Record<string, string> = {
    accept: "text/event-stream",
    "content-type": "application/json",
  };
  const body = JSON.stringify({ query, ...(model ? { model } : {}) });

  const response = await fetch(`/api/backend/v1/workspaces/${workspaceId}/ask/stream`, {
    method: "POST",
    headers,
    body,
    signal,
    cache: "no-store",
  });

  if (!response.ok) {
    // M5-001a REV-2 Finding 4: a non-2xx response may still carry an SSE
    // `event: error` body (the backend emits the error envelope as a frame,
    // not as a JSON object). Parse the body per content-type and surface the
    // frame's code/message/trace_id instead of collapsing to http_<status>.
    const sseError = await readSseError(response);
    if (sseError) {
      // The turnId travels on EVERY event (REV-3 residual): the page's
      // turn-scope guard drops an error frame without it, so a 403/503 would
      // leave a blank assistant message instead of a displayed failure.
      yield {
        type: "error",
        code: sseError.code,
        message: sseError.message,
        traceId: sseError.traceId,
        turnId,
      };
      return;
    }
    // Parse error envelope if present
    let code = `http_${response.status}`;
    let message = `Request failed (HTTP ${response.status}).`;
    try {
      const err = await response.json();
      const rec = isRecord(err) ? err : null;
      if (rec?.error) {
        const e = isRecord(rec.error) ? rec.error : null;
        if (typeof e?.code === "string") code = e.code;
        if (typeof e?.message === "string") message = e.message;
      }
    } catch {
      // Leave defaults
    }
    yield { type: "error", code, message, turnId };
    return;
  }

  if (!response.body) {
    yield { type: "error", code: "no_body", message: "Server returned no stream body.", turnId };
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  const queue: EventSourceMessage[] = [];
  let deferred: Promise<void> | null = null;
  let resolveDeferred: (() => void) | null = null;
  function ensureDeferred(): Promise<void> {
    if (!deferred) {
      deferred = new Promise<void>((r) => {
        resolveDeferred = r;
      });
    }
    return deferred;
  }
  function wake(): void {
    if (resolveDeferred) {
      const r = resolveDeferred;
      resolveDeferred = null;
      deferred = null;
      r();
    }
  }

  const parser = createParser({
    onEvent(event: EventSourceMessage) {
      queue.push(event);
      wake();
    },
    onError(err: unknown) {
      const e = err as { type?: string; message?: string };
      if (e.type !== "max-buffer-size-exceeded") return;
      queue.push({ event: "error", data: JSON.stringify({ code: "parse_error", message: e.message ?? "SSE parse error" }) });
      wake();
    },
  });

  /**
   * M5-001a REV-2 Finding 3: the detached reader pump previously had no
   * rejection handler — an errored reader produced an unhandled AbortError
   * while the iterator completed normally. Read failures now travel through
   * the queue as an explicit error frame, and intentional abort is handled
   * SEPARATELY (abort = clean stop, not an error event).
   */
  async function pump() {
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        if (value) parser.feed(decoder.decode(value, { stream: true }));
      }
    } catch (error: unknown) {
      // Abort = clean stop (user pressed Stop, or the caller's signal fired).
      // No error event, no unhandled rejection — the generator just ends.
      if (isAbortError(error)) {
        queue.push({ event: "__done__", data: "" });
        wake();
        return;
      }
      // Any other read failure surfaces to the consumer as an error event,
      // then the generator ends cleanly (the sentinel is what terminates the
      // iterator — without it the loop would hang on the next ensureDeferred).
      queue.push({
        event: "error",
        data: JSON.stringify({
          code: "stream_read_error",
          message: error instanceof Error ? error.message : "Stream read failed.",
        }),
      });
      queue.push({ event: "__done__", data: "" });
      wake();
      return;
    }
    // Normal completion path: signal the iterator that the stream is done.
    queue.push({ event: "__done__", data: "" });
    wake();
  }
  void pump(); // fire-and-forget; the generator drives completion via queue

  try {
    while (true) {
      if (queue.length === 0) await ensureDeferred();
      const event = queue.shift();
      if (!event) continue;
      if (event.event === "__done__") break;
      if (!event.data) continue;

      const parsed = parseData(event.data);
      if (!isRecord(parsed)) continue;

      switch (event.event) {
        case "token": {
          const text = parsed.text;
          if (typeof text === "string") {
            yield { type: "token", text, turnId };
          }
          break;
        }
        case "citations": {
          const answer = parsed as AnswerPayload;
          if (answer && typeof answer.answer === "string") {
            yield { type: "citations", answer, turnId };
          }
          break;
        }
        case "error": {
          const code =
            typeof parsed.code === "string" ? parsed.code : "stream_error";
          const message =
            typeof parsed.error === "string"
              ? parsed.error
              : typeof parsed.message === "string"
                ? parsed.message
                : "Streaming answer failed.";
          yield {
            type: "error",
            code,
            message,
            traceId: typeof parsed.trace_id === "string" ? parsed.trace_id : null,
            turnId,
          };
          break;
        }
      }
    }
  } finally {
    reader.cancel().catch(() => undefined);
  }
}
