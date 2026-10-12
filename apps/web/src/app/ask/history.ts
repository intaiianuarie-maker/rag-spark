"use client";

import type { AnswerStatus, Claim, Confidence } from "./contracts";
import type { ChatMessage, MessageCitation } from "./components/chat-window";

/**
 * Ask transcript persistence (T-V1-033 — RP-13/QF-13).
 *
 * Before this file the transcript lived ONLY in `useState` on the Ask page:
 * a refresh threw it away and a workspace switch showed whichever workspace
 * you had used last. This module is the persistence layer the page talks to:
 *
 *  - the transcript is written to a durable key/value store (Web Storage),
 *    so a refresh — or a full browser restart — restores it;
 *  - the storage KEY is namespaced by account AND workspace, and the payload
 *    repeats both, so history written under workspace A (or account A) can
 *    never be read back as workspace B's history;
 *  - every read is validated: an unknown schema version, a foreign scope or
 *    a corrupt payload yields an empty transcript instead of crashing the
 *    page or rendering someone else's messages.
 *
 * Deliberate limits, disclosed rather than hidden:
 *  - scope is per-browser (Web Storage), not a server-side conversation
 *    store. Cross-device history would need a backend table + migration,
 *    which is outside this ticket's allowed files.
 *  - a transcript is bounded (`MAX_PERSISTED_MESSAGES`) so the store can
 *    never grow without limit.
 *  - storage may be unavailable (private mode, quota, SSR): the page then
 *    keeps working with an in-memory transcript for the session — the
 *    helpers return "not persisted" rather than throwing.
 *
 * Nothing here is authorization: the scope only namespaces a browser-local
 * cache. ACL for the CONTENT of these messages was enforced by the backend
 * when the answer was produced (R-004).
 */

/** Bump when the envelope shape changes; older payloads are dropped. */
export const HISTORY_SCHEMA_VERSION = 1;

/** Key namespace. The version segment keeps old payloads unreachable. */
export const HISTORY_KEY_PREFIX = "kios_ask_history";

/** Hard bound on how many turns are ever written back. */
export const MAX_PERSISTED_MESSAGES = 200;

/**
 * Account + workspace the transcript belongs to. Both parts are required:
 * two accounts sharing one workspace on the same browser must not read each
 * other's transcripts.
 */
export type HistoryScope = {
  account: string;
  workspace: string;
};

export type HistoryEnvelope = {
  version: number;
  account: string;
  workspace: string;
  saved_at: string;
  messages: ChatMessage[];
};

/** Structural subset of `Storage` so tests can inject a fake. */
export interface HistoryStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/**
 * Scope part validator. Ids are uuid/uuid-like, so anything outside this
 * charset (":", "/", whitespace, control characters) is rejected outright
 * rather than encoded — a scope that cannot be written into a key is simply
 * not persisted, which keeps key collisions and cross-scope reads impossible.
 */
const SCOPE_PART = /^[A-Za-z0-9._@+-]{1,128}$/;

/** Storage key for a scope, or null when the scope is not key-safe. */
export function historyStorageKey(scope: HistoryScope): string | null {
  if (!SCOPE_PART.test(scope.account) || !SCOPE_PART.test(scope.workspace)) return null;
  return `${HISTORY_KEY_PREFIX}:v${HISTORY_SCHEMA_VERSION}:${scope.account}:${scope.workspace}`;
}

/**
 * The browser store, or null when it is unusable. The write probe matters:
 * Safari private mode exposes `localStorage` but throws on `setItem`.
 */
export function browserStorage(): HistoryStorage | null {
  try {
    const store = globalThis.localStorage;
    if (!store) return null;
    const probe = `${HISTORY_KEY_PREFIX}:__probe`;
    store.setItem(probe, "1");
    store.removeItem(probe);
    return store;
  } catch {
    return null;
  }
}

const ANSWER_STATUSES: readonly string[] = [
  "answered",
  "partial",
  "insufficient_evidence",
  "clarification_needed",
  "unavailable",
];

/** Mirror of the `AnswerStatus` union in `./contracts` (kept type-safe). */
function isAnswerStatus(value: unknown): value is AnswerStatus {
  return typeof value === "string" && ANSWER_STATUSES.includes(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function optionalString(value: unknown): string | null | undefined {
  if (value === undefined) return undefined;
  if (value === null) return null;
  return typeof value === "string" ? value : undefined;
}

function stringList(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  return value.every((item) => typeof item === "string") ? (value as string[]) : null;
}

function normalizeClaims(value: unknown): Claim[] | null {
  if (!Array.isArray(value)) return null;
  const claims: Claim[] = [];
  for (const item of value) {
    if (!isRecord(item)) return null;
    const text = item.text;
    const chunkIds = stringList(item.chunk_ids);
    if (typeof text !== "string" || chunkIds === null) return null;
    claims.push({ text, chunk_ids: chunkIds });
  }
  return claims;
}

function normalizeCitations(value: unknown): MessageCitation[] | null {
  if (!Array.isArray(value)) return null;
  const citations: MessageCitation[] = [];
  for (const item of value) {
    if (!isRecord(item)) return null;
    const chunkId = item.chunk_id;
    if (typeof chunkId !== "string" || chunkId.length === 0) return null;
    const citation: MessageCitation = {
      chunk_id: chunkId,
      document_id: typeof item.document_id === "string" ? item.document_id : null,
      filename: typeof item.filename === "string" ? item.filename : null,
      page: typeof item.page === "number" && Number.isFinite(item.page) ? item.page : null,
      section: typeof item.section === "string" ? item.section : null,
      span: isSpan(item.span) ? item.span : null,
      workspace_id: typeof item.workspace_id === "string" ? item.workspace_id : null,
    };
    const content = optionalString(item.content);
    if (content !== undefined) citation.content = content;
    if (isBbox(item.bbox)) citation.bbox = item.bbox;
    if (typeof item.grounding_score === "number" && Number.isFinite(item.grounding_score)) {
      citation.grounding_score = item.grounding_score;
    }
    citations.push(citation);
  }
  return citations;
}

function isSpan(value: unknown): value is [number, number] {
  return (
    Array.isArray(value) &&
    value.length === 2 &&
    typeof value[0] === "number" &&
    typeof value[1] === "number" &&
    Number.isFinite(value[0]) &&
    Number.isFinite(value[1])
  );
}

function isBbox(value: unknown): value is [number, number, number, number] {
  return (
    Array.isArray(value) &&
    value.length === 4 &&
    value.every((n) => typeof n === "number" && Number.isFinite(n))
  );
}

function normalizeConfidence(value: unknown): Confidence | undefined {
  if (!isRecord(value)) return undefined;
  const level = value.level;
  const basis = value.basis;
  if (
    level !== "high" &&
    level !== "medium" &&
    level !== "low" &&
    level !== "unavailable"
  ) {
    return undefined;
  }
  if (typeof basis !== "string") return undefined;
  const probability =
    typeof value.probability === "number" && Number.isFinite(value.probability)
      ? value.probability
      : null;
  return { level, basis, probability };
}

/**
 * Validate + normalize one persisted turn. Returns null only when the turn is
 * not a usable message at all (wrong role, non-text content, malformed
 * claims/citations) — that turn is then DROPPED, never guessed at.
 */
export function normalizeMessage(value: unknown): ChatMessage | null {
  if (!isRecord(value)) return null;
  if (value.role !== "user" && value.role !== "assistant") return null;
  if (typeof value.content !== "string") return null;
  const claims = normalizeClaims(value.claims);
  const citations = normalizeCitations(value.citations);
  if (claims === null || citations === null) return null;

  const message: ChatMessage = {
    role: value.role,
    content: value.content,
    claims,
    citations,
  };

  if (isAnswerStatus(value.status)) message.status = value.status;
  if (typeof value.reason_code === "string") message.reason_code = value.reason_code;
  else if (value.reason_code === null) message.reason_code = null;
  if (typeof value.reason === "string") message.reason = value.reason;
  else if (value.reason === null) message.reason = null;

  const confidence = normalizeConfidence(value.confidence);
  if (confidence !== undefined) message.confidence = confidence;

  const limitations = stringList(value.limitations);
  if (limitations !== null) message.limitations = limitations;

  if (typeof value.error === "boolean") message.error = value.error;
  return message;
}

/**
 * Read an envelope for `scope`. Every mismatch returns `[]`:
 *  - wrong schema version → the shape is not understood;
 *  - payload account/workspace differs from the requested scope → this is
 *    another scope's transcript (the cross-workspace/cross-account guard);
 *  - malformed payload or messages → those messages are dropped.
 */
export function readEnvelope(value: unknown, scope: HistoryScope): ChatMessage[] {
  if (!isRecord(value)) return [];
  if (value.version !== HISTORY_SCHEMA_VERSION) return [];
  if (value.account !== scope.account || value.workspace !== scope.workspace) return [];
  if (!Array.isArray(value.messages)) return [];
  const messages = value.messages
    .map(normalizeMessage)
    .filter((message): message is ChatMessage => message !== null);
  return messages.slice(-MAX_PERSISTED_MESSAGES);
}

/** Restore the transcript for a scope; `[]` whenever nothing valid is stored. */
export function loadHistory(
  storage: HistoryStorage | null,
  scope: HistoryScope,
): ChatMessage[] {
  const key = historyStorageKey(scope);
  if (storage === null || key === null) return [];
  let raw: string | null = null;
  try {
    raw = storage.getItem(key);
  } catch {
    return [];
  }
  if (raw === null) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  return readEnvelope(parsed, scope);
}

/**
 * Persist a transcript for a scope. Returns false when nothing was written
 * (no usable store, key-unsafe scope, quota exceeded) — callers treat that
 * as "history is in-memory only this session", never as an error.
 */
export function saveHistory(
  storage: HistoryStorage | null,
  scope: HistoryScope,
  messages: ChatMessage[],
): boolean {
  const key = historyStorageKey(scope);
  if (storage === null || key === null) return false;
  const bounded = messages.slice(-MAX_PERSISTED_MESSAGES);
  const envelope: HistoryEnvelope = {
    version: HISTORY_SCHEMA_VERSION,
    account: scope.account,
    workspace: scope.workspace,
    saved_at: new Date().toISOString(),
    messages: bounded,
  };
  try {
    storage.setItem(key, JSON.stringify(envelope));
    return true;
  } catch {
    return false;
  }
}

/** Drop one scope's transcript (used when a session/account changes). */
export function clearHistory(storage: HistoryStorage | null, scope: HistoryScope): void {
  const key = historyStorageKey(scope);
  if (storage === null || key === null) return;
  try {
    storage.removeItem(key);
  } catch {
    // Nothing to do: an unusable store is already equivalent to "cleared".
  }
}
