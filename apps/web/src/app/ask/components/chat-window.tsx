"use client";

import { useState } from "react";
import type { AnswerPayload, AnswerStatus, Claim, Citation, Confidence } from "../contracts";
import { CitationChip } from "./citation-chip";

/**
 * Citation exactly as it is stored WITH the message that produced it
 * (T-V1-032 — RP-13/QF-13). Nothing here is shared across messages: each
 * assistant message owns its own array, so answer #2 can never make answer
 * #1's chips resolve to answer #2's documents.
 *
 * `content` / `bbox` / `grounding_score` are additive, optional fields the
 * backend `Citation` model already serializes (packages/answer/models.py) but
 * the frozen ask contract type does not declare. They are declared locally
 * instead of editing `../contracts` so the ticket's allowed-file list stays
 * honored.
 */
export type MessageCitation = Citation & {
  /** Canonical passage text hydrated server-side when the answer was built. */
  content?: string | null;
  /** Optional page-space box `[x0, y0, x1, y1]`, when the source provides it. */
  bbox?: [number, number, number, number] | null;
  /** Grounding score in [0, 1], when a grounder scored this citation. */
  grounding_score?: number | null;
};

/**
 * One turn of the transcript. Assistant messages carry the FULL answer
 * truth — status, reason, confidence, limitations, claims AND citations —
 * frozen at the moment that answer was received.
 */
export type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  claims: Claim[];
  citations: MessageCitation[];
  status?: AnswerStatus;
  reason_code?: string | null;
  reason?: string | null;
  confidence?: Confidence;
  limitations?: string[];
  /** True when the turn failed client-side: no answer payload, no citations. */
  error?: boolean;
};

/** A user turn. Deliberately owns an EMPTY citation list, never a borrowed one. */
export function userMessage(query: string): ChatMessage {
  return { role: "user", content: query, claims: [], citations: [] };
}

/**
 * Freeze an answer payload into its own message (T-V1-032 instruction 1):
 * citations + confidence + limitations + status are copied onto THIS message
 * and are never read back from a "latest answer" slot.
 */
export function answerMessage(payload: AnswerPayload): ChatMessage {
  return {
    role: "assistant",
    content: payload.answer,
    // Arrays are COPIED, not aliased: the message owns its own list, so a
    // later mutation (or a frozen payload) can never rewrite what this
    // message cites.
    claims: [...(payload.claims ?? [])],
    citations: [...(payload.citations ?? [])],
    status: payload.status,
    reason_code: payload.reason_code ?? null,
    reason: payload.reason ?? null,
    confidence: payload.confidence,
    limitations: [...(payload.limitations ?? [])],
    error: false,
  };
}

/**
 * A failed turn. It carries NO citations, so a transport/API error can never
 * visually re-use the previous successful answer's evidence.
 */
export function failureMessage(error: unknown): ChatMessage {
  const text = error instanceof Error ? error.message : "Something went wrong.";
  return {
    role: "assistant",
    content: `Error: ${text}`,
    claims: [],
    citations: [],
    error: true,
    limitations: [],
  };
}

/**
 * Resolve a claim's chunk id against ONE message's citations. Returns null
 * when the chunk is not part of that message — the cross-message mixup guard.
 */
export function citationForChunk(
  message: ChatMessage,
  chunkId: string,
): MessageCitation | null {
  return message.citations.find((citation) => citation.chunk_id === chunkId) ?? null;
}

const STATUS_LABEL: Record<AnswerStatus, string> = {
  answered: "Answered",
  partial: "Partial answer",
  insufficient_evidence: "Insufficient evidence",
  clarification_needed: "Clarification needed",
  unavailable: "Unavailable",
  // T-V1-025 taxonomy states — labelled, never rendered as a clean answer.
  degraded: "Degraded (optional capability down)",
  needs_review: "Needs review",
};

const STATUS_CLASS: Record<AnswerStatus, string> = {
  answered: "bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-900/20 dark:text-emerald-300 dark:ring-emerald-700/40",
  partial: "bg-amber-50 text-amber-700 ring-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:ring-amber-700/40",
  insufficient_evidence: "bg-neutral-100 text-neutral-600 ring-neutral-200 dark:bg-neutral-800 dark:text-neutral-300 dark:ring-neutral-700",
  clarification_needed: "bg-sky-50 text-sky-700 ring-sky-200 dark:bg-sky-900/20 dark:text-sky-300 dark:ring-sky-700/40",
  unavailable: "bg-red-50 text-red-700 ring-red-200 dark:bg-red-900/20 dark:text-red-300 dark:ring-red-700/40",
  degraded: "bg-amber-50 text-amber-800 ring-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:ring-amber-700/40",
  needs_review: "bg-violet-50 text-violet-700 ring-violet-200 dark:bg-violet-900/20 dark:text-violet-300 dark:ring-violet-700/40",
};

/** Answer-truth block (status / confidence / limitations) for ONE message. */
function MessageMeta({ message }: { message: ChatMessage }) {
  if (message.error) return null;
  const hasConfidence = message.confidence !== undefined;
  const hasLimitations = (message.limitations?.length ?? 0) > 0;
  const hasStatus = message.status !== undefined;
  if (!hasStatus && !hasConfidence && !hasLimitations && !message.reason) return null;

  return (
    <div className="mt-3 space-y-2 border-t border-neutral-200 pt-3 text-xs dark:border-neutral-800">
      <div className="flex flex-wrap items-center gap-2">
        {message.status && (
          <span
            className={`inline-flex rounded-full px-2 py-0.5 font-medium ring-1 ring-inset ${STATUS_CLASS[message.status]}`}
          >
            {STATUS_LABEL[message.status]}
          </span>
        )}
        {message.confidence && (
          <span className="text-neutral-500 dark:text-neutral-400">
            Confidence:{" "}
            <span className="font-medium text-neutral-700 dark:text-neutral-300">
              {message.confidence.level}
            </span>
            {typeof message.confidence.probability === "number" &&
              ` (${message.confidence.probability.toFixed(2)})`}
          </span>
        )}
      </div>
      {message.confidence?.basis && (
        <p className="text-neutral-500 dark:text-neutral-400">{message.confidence.basis}</p>
      )}
      {message.reason && (
        <p className="text-neutral-500 dark:text-neutral-400">{message.reason}</p>
      )}
      {hasLimitations && (
        <div>
          <span className="text-[10px] font-semibold uppercase tracking-wider text-neutral-400">
            Limitations
          </span>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-neutral-600 dark:text-neutral-400">
            {message.limitations?.map((limitation, index) => (
              <li key={index}>{limitation}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/**
 * Copy shown while the request the user sent has not been answered. It
 * states the fact (an answer is pending) and nothing about how the answer
 * will arrive.
 */
export const PENDING_LABEL = "Waiting for the answer…";

export function ChatWindow({
  pending,
  messages,
  onSend,
  onOpenSource,
}: {
  /**
   * True while a request the user sent has not been answered yet.
   *
   * Named `pending` on purpose (T-V1-033): the backend computes the whole
   * answer and the browser receives it in one response, so this flag must
   * not be dressed up as a feature the transport does not have. It gates the
   * input and shows `PENDING_LABEL` — that is the entire claim.
   */
  pending: boolean;
  messages: ChatMessage[];
  onSend: (text: string) => void;
  onOpenSource: (c: MessageCitation) => void;
}) {
  const [input, setInput] = useState("");

  return (
    <div className="flex flex-1 flex-col">
      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto max-w-3xl space-y-6">
          {messages.map((msg, i) => (
            <div key={i} className="space-y-3">
              <div className="text-xs font-semibold uppercase tracking-wide text-neutral-400">
                {msg.role === "user" ? "You" : "Assistant"}
              </div>
              <div className="rounded-xl bg-neutral-50 p-4 text-neutral-800 dark:bg-neutral-900 dark:text-neutral-200">
                <p className="whitespace-pre-wrap leading-relaxed">{msg.content}</p>
                {msg.claims.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {msg.claims.map((claim, ci) => (
                      <div key={ci} className="space-y-1">
                        <p className="text-sm">{claim.text}</p>
                        <div className="flex flex-wrap gap-1.5">
                          {claim.chunk_ids.map((chunkId) => {
                            // OWN citations only (T-V1-032): a chunk id that is
                            // not on THIS message renders no chip at all.
                            const cit = citationForChunk(msg, chunkId);
                            if (!cit) return null;
                            return (
                              <CitationChip key={chunkId} citation={cit} onOpen={onOpenSource} />
                            );
                          })}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                <MessageMeta message={msg} />
              </div>
            </div>
          ))}
          {pending && (
            <div
              className="rounded-xl bg-neutral-50 p-4 text-neutral-500 dark:bg-neutral-900 dark:text-neutral-400"
              role="status"
              aria-live="polite"
            >
              {PENDING_LABEL}
            </div>
          )}
        </div>
      </div>
      <div className="border-t border-neutral-200 px-6 py-4 dark:border-neutral-800">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!input.trim() || pending) return;
            onSend(input.trim());
            setInput("");
          }}
          className="mx-auto max-w-3xl flex gap-2"
        >
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask anything…"
            className="flex-1 rounded-lg border border-neutral-300 bg-white px-4 py-2.5 text-sm text-neutral-900 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-100"
            disabled={pending}
          />
          <button
            type="submit"
            disabled={!input.trim() || pending}
            className="rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Send
          </button>
        </form>
      </div>
    </div>
  );
}
