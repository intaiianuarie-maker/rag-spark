"use client";

import { useEffect, useState } from "react";
import type { MessageCitation } from "./chat-window";

/**
 * Source panel for the Ask screen (T-V1-032 — RP-13/QF-13).
 *
 * Two responsibilities:
 *  1. Show the provenance of ONE citation (the one that was clicked — never
 *     a value derived from whatever the latest answer happens to be).
 *  2. Fetch the AUTHORIZED CANONICAL passage for that citation through the
 *     single passage-fetch route (`/api/passage`, which forwards to the
 *     backend with the caller's bearer token so the ACL check stays server
 *     side) and highlight the cited span.
 *
 * Honest degradation: the backend canonical-source endpoint is T-V1-022's
 * deliverable and may 404 until it lands. That is surfaced EXPLICITLY
 * (`notice` / `error` state with the upstream code) instead of being hidden
 * or silently swapped for another message's source.
 */

/** Normalized shape returned by the passage-fetch route. */
export type PassagePayload = {
  chunk_id?: string | null;
  content?: string | null;
  document_id?: string | null;
  filename?: string | null;
  page?: number | null;
  section?: string | null;
  span?: [number, number] | null;
  bbox?: [number, number, number, number] | null;
  workspace_id?: string | null;
};

/** Where the rendered passage text came from — always disclosed in the UI. */
export type PassageOrigin = "canonical" | "answer_embedded";

export type PassageResult =
  | { ok: true; text: string; passage: PassagePayload | null }
  | { ok: false; code: string; message: string };

export type PassageView =
  | { state: "loading" }
  | { state: "ready"; text: string; origin: PassageOrigin; notice: string | null }
  | { state: "error"; code: string; message: string }
  | { state: "empty"; notice: string | null };

/** Same-origin passage fetch; `chunk_id`/`workspace_id` are percent-encoded. */
export function buildPassageUrl(workspaceId: string, chunkId: string): string {
  const params = new URLSearchParams({ workspace_id: workspaceId, chunk_id: chunkId });
  return `/api/passage?${params.toString()}`;
}

type Fetcher = (input: string, init?: RequestInit) => Promise<Response>;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Passage request failed.";
}

/**
 * Call the passage-fetch route and normalize BOTH outcomes: a 2xx body with
 * passage text, or a failure carrying the route's explicit `{error:{code,…
 * message}}` envelope. Network/parse failures are folded into the same
 * failure shape so callers only ever branch on `ok`.
 */
export async function requestPassage(
  workspaceId: string,
  chunkId: string,
  fetcher?: Fetcher,
): Promise<PassageResult> {
  const doFetch: Fetcher = fetcher ?? ((input, init) => globalThis.fetch(input, init));
  let response: Response;
  try {
    response = await doFetch(buildPassageUrl(workspaceId, chunkId), {
      method: "GET",
      cache: "no-store",
      headers: { accept: "application/json" },
    });
  } catch (error) {
    return { ok: false, code: "network_error", message: errorMessage(error) };
  }

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const record =
    typeof body === "object" && body !== null ? (body as Record<string, unknown>) : null;

  if (!response.ok) {
    const envelope = record?.error as Record<string, unknown> | undefined;
    const code =
      typeof envelope?.code === "string" ? envelope.code : `http_${response.status}`;
    const message =
      typeof envelope?.message === "string"
        ? envelope.message
        : `Passage fetch failed (HTTP ${response.status}).`;
    return { ok: false, code, message };
  }

  const passage = record?.passage as PassagePayload | undefined;
  const text = typeof passage?.content === "string" ? passage.content : null;
  if (text === null) {
    return {
      ok: false,
      code: "passage_malformed",
      message: "Backend returned a passage without readable text.",
    };
  }
  return { ok: true, text, passage: passage ?? null };
}

/**
 * Turn a fetch result into what the panel renders, keeping the citation's OWN
 * answer-embedded text as the last resort:
 *
 *  - canonical 200 with text → live canonical passage;
 *  - canonical 200 but empty, or fetch failed → the passage recorded with
 *    THIS citation, always labeled `answer_embedded` WITH the failure notice;
 *  - nothing to show → explicit `error` / `empty` state (never another
 *    message's source — the citation object is the only input).
 */
export function resolvePassage(result: PassageResult, citation: MessageCitation): PassageView {
  const embedded =
    typeof citation.content === "string" && citation.content.trim().length > 0
      ? citation.content
      : null;

  if (result.ok) {
    if (result.text.trim().length > 0) {
      return { state: "ready", text: result.text, origin: "canonical", notice: null };
    }
    if (embedded !== null) {
      return {
        state: "ready",
        text: embedded,
        origin: "answer_embedded",
        notice: "Canonical passage was empty; showing the passage recorded with this answer.",
      };
    }
    return {
      state: "empty",
      notice: "Canonical passage is empty and this citation recorded no text.",
    };
  }

  if (embedded !== null) {
    return {
      state: "ready",
      text: embedded,
      origin: "answer_embedded",
      notice: `${result.code}: ${result.message}`,
    };
  }
  return { state: "error", code: result.code, message: result.message };
}

export type PassageSegment = { text: string; hit: boolean };

/**
 * Split a passage on a character span `[start, end)` so the cited characters
 * can be wrapped in a `<mark>`. Out-of-range offsets are clamped (never
 * throw), inverted spans produce no highlight, and a missing span returns the
 * whole text unhighlighted.
 */
export function segmentPassage(
  text: string,
  span: [number, number] | null | undefined,
): PassageSegment[] {
  if (text.length === 0) return [];
  if (!span) return [{ text, hit: false }];

  const start = Math.max(0, Math.min(text.length, Math.floor(span[0])));
  const end = Math.max(0, Math.min(text.length, Math.floor(span[1])));
  if (!Number.isFinite(span[0]) || !Number.isFinite(span[1]) || end <= start) {
    return [{ text, hit: false }];
  }

  const segments: PassageSegment[] = [];
  if (start > 0) segments.push({ text: text.slice(0, start), hit: false });
  segments.push({ text: text.slice(start, end), hit: true });
  if (end < text.length) segments.push({ text: text.slice(end), hit: false });
  return segments;
}

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="text-xs uppercase tracking-wider text-neutral-400">{label}</span>
      <p className="font-medium break-words">{value}</p>
    </div>
  );
}

export function SourcePanel({
  citation,
  workspaceId,
  onClose,
}: {
  citation: MessageCitation | null;
  workspaceId?: string | null;
  onClose: () => void;
}) {
  // Last completed fetch, keyed by the refs it was made for. Comparing the
  // key (instead of resetting state in the effect) is what renders the
  // loading state: a key mismatch means the fetch for the clicked citation
  // has not landed yet.
  const [fetchState, setFetchState] = useState<{
    key: string;
    result: PassageResult;
  } | null>(null);

  const scope = citation ? citation.workspace_id ?? workspaceId ?? null : null;
  const chunkId = citation?.chunk_id ?? null;
  const passageKey =
    citation !== null && scope !== null && chunkId !== null
      ? `${scope}::${chunkId}`
      : null;

  useEffect(() => {
    if (!citation || passageKey === null || scope === null || chunkId === null) return;
    let cancelled = false;

    requestPassage(scope, chunkId)
      .then((result) => {
        if (!cancelled) setFetchState({ key: passageKey, result });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setFetchState({
            key: passageKey,
            result: { ok: false, code: "network_error", message: errorMessage(error) },
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [citation, workspaceId, passageKey, scope, chunkId]);

  const view: PassageView =
    citation === null
      ? { state: "loading" }
      : passageKey === null || scope === null || chunkId === null
        ? resolvePassage(
            {
              ok: false,
              code: "missing_reference",
              message: "This citation has no chunk/workspace reference to fetch.",
            },
            citation,
          )
        : fetchState !== null && fetchState.key === passageKey
          ? resolvePassage(fetchState.result, citation)
          : { state: "loading" };

  if (!citation) return null;

  const span = citation.span;
  const segments = view.state === "ready" ? segmentPassage(view.text, span) : [];

  return (
    <aside className="w-full md:w-80 md:shrink-0 md:border-l md:border-neutral-200 md:pl-6 md:dark:border-neutral-800">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-neutral-800 dark:text-neutral-200">Source</h3>
        <button
          type="button"
          onClick={onClose}
          className="text-xs text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300"
          aria-label="Close source panel"
        >
          Close
        </button>
      </div>
      <div className="space-y-3 text-sm text-neutral-700 dark:text-neutral-300">
        <MetaRow
          label="Document"
          value={citation.filename || citation.document_id || citation.chunk_id}
        />
        <div className="flex gap-4">
          <div>
            <span className="text-xs uppercase tracking-wider text-neutral-400">Page</span>
            <p className="font-medium">{citation.page ?? "—"}</p>
          </div>
          <div>
            <span className="text-xs uppercase tracking-wider text-neutral-400">Section</span>
            <p className="font-medium">{citation.section ?? "—"}</p>
          </div>
        </div>
        {span && (
          <div>
            <span className="text-xs uppercase tracking-wider text-neutral-400">Span</span>
            <p className="font-mono text-xs">({span[0]}, {span[1]})</p>
          </div>
        )}
        {citation.bbox && (
          <div>
            <span className="text-xs uppercase tracking-wider text-neutral-400">BBox</span>
            <p className="font-mono text-xs">
              ({citation.bbox[0]}, {citation.bbox[1]}, {citation.bbox[2]}, {citation.bbox[3]})
            </p>
          </div>
        )}

        <div>
          <span className="text-xs uppercase tracking-wider text-neutral-400">Passage</span>

          {view.state === "loading" && (
            <p
              className="mt-1 animate-pulse text-sm text-neutral-500 dark:text-neutral-400"
              role="status"
              aria-live="polite"
            >
              Loading passage…
            </p>
          )}

          {view.state === "error" && (
            <div
              className="mt-1 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300"
              role="alert"
            >
              <p className="font-medium">Passage unavailable ({view.code})</p>
              <p className="mt-1 text-xs">{view.message}</p>
            </div>
          )}

          {view.state === "empty" && (
            <div
              className="mt-1 rounded-lg border border-neutral-200 bg-neutral-100 p-3 text-sm text-neutral-600 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-400"
              role="status"
            >
              <p className="font-medium">No passage text</p>
              <p className="mt-1 text-xs">{view.notice}</p>
            </div>
          )}

          {view.state === "ready" && (
            <>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] font-semibold uppercase tracking-wider">
                <span
                  className={
                    view.origin === "canonical"
                      ? "rounded-full bg-emerald-50 px-2 py-0.5 text-emerald-700 ring-1 ring-inset ring-emerald-200 dark:bg-emerald-900/20 dark:text-emerald-300 dark:ring-emerald-700/40"
                      : "rounded-full bg-amber-50 px-2 py-0.5 text-amber-700 ring-1 ring-inset ring-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:ring-amber-700/40"
                  }
                  title={
                    view.origin === "canonical"
                      ? "Fetched live from the backend canonical source, ACL-checked for this caller"
                      : "Passage recorded with this answer; live canonical fetch did not return text"
                  }
                >
                  {view.origin === "canonical" ? "Canonical (live)" : "Answer-embedded"}
                </span>
              </div>
              {view.notice && (
                <p
                  className="mt-1 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs text-amber-700 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-300"
                  role="status"
                >
                  {view.notice}
                </p>
              )}
              <p className="mt-1 whitespace-pre-wrap rounded-lg bg-neutral-100 p-3 text-sm leading-relaxed dark:bg-neutral-900">
                {segments.map((segment, index) =>
                  segment.hit ? (
                    <mark
                      key={index}
                      className="rounded-sm bg-yellow-200 px-0.5 text-neutral-900 dark:bg-yellow-500/70 dark:text-neutral-950"
                    >
                      {segment.text}
                    </mark>
                  ) : (
                    <span key={index}>{segment.text}</span>
                  ),
                )}
              </p>
            </>
          )}
        </div>
      </div>
    </aside>
  );
}
