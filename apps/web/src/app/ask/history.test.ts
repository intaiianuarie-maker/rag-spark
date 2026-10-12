/**
 * T-V1-033 — Ask transcript persistence (RP-13/QF-13).
 *
 * The three properties this ticket exists for:
 *  (1) refresh restores the transcript (it is NOT React-state-only);
 *  (2) the transcript is scoped to account AND workspace — a workspace
 *      switch re-scopes and nothing leaks across scopes;
 *  (3) deleted/unavailable sources keep resolving against their OWN message.
 */
import { describe, expect, it } from "vitest";

import {
  HISTORY_SCHEMA_VERSION,
  MAX_PERSISTED_MESSAGES,
  clearHistory,
  historyStorageKey,
  loadHistory,
  normalizeMessage,
  readEnvelope,
  saveHistory,
  type HistoryScope,
  type HistoryStorage,
} from "./history";
import { answerMessage, citationForChunk, userMessage } from "./components/chat-window";
import type { AnswerPayload } from "./contracts";

const ACCOUNT_A = "11111111-1111-4111-8111-111111111111";
const ACCOUNT_B = "22222222-2222-4222-8222-222222222222";
const WS_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const WS_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const scopeA: HistoryScope = { account: ACCOUNT_A, workspace: WS_A };

/** In-memory store with failure switches, so error paths are real. */
function fakeStorage(): HistoryStorage & {
  raw(key: string): string | null;
  put(key: string, value: string): void;
  keys(): string[];
} {
  const map = new Map<string, string>();
  return {
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => void map.set(key, value),
    removeItem: (key) => void map.delete(key),
    raw: (key) => map.get(key) ?? null,
    put: (key, value) => void map.set(key, value),
    keys: () => [...map.keys()],
  };
}

function payload(overrides: Partial<AnswerPayload> = {}): AnswerPayload {
  return {
    answer: "Alpha answer.",
    status: "answered",
    claims: [{ text: "Alpha claim.", chunk_ids: ["chunk-a"] }],
    citations: [
      {
        chunk_id: "chunk-a",
        document_id: "doc-a",
        filename: "alpha.pdf",
        page: 1,
        section: null,
        span: [0, 5],
        workspace_id: WS_A,
      },
    ],
    ...overrides,
  };
}

describe("refresh restores history (not React-state-only)", () => {
  it("round-trips a transcript through the store", () => {
    const storage = fakeStorage();
    const transcript = [userMessage("what is RP-13?"), answerMessage(payload())];

    expect(saveHistory(storage, scopeA, transcript)).toBe(true);

    // A refresh is a brand-new render with no component state: only the
    // store survives.
    const restored = loadHistory(storage, scopeA);
    expect(restored).toHaveLength(2);
    expect(restored.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(restored[1].content).toBe("Alpha answer.");
    expect(restored[1].status).toBe("answered");
    expect(restored[1].claims).toEqual([{ text: "Alpha claim.", chunk_ids: ["chunk-a"] }]);
    expect(restored[1].citations[0]).toMatchObject({
      chunk_id: "chunk-a",
      filename: "alpha.pdf",
      workspace_id: WS_A,
    });
  });

  it("restored citations stay with their own message", () => {
    const storage = fakeStorage();
    const first = answerMessage(payload());
    const second = answerMessage(
      payload({
        answer: "Beta answer.",
        claims: [{ text: "Beta claim.", chunk_ids: ["chunk-b"] }],
        citations: [
          {
            chunk_id: "chunk-b",
            document_id: "doc-b",
            filename: "beta.pdf",
            page: 2,
            section: null,
            span: null,
            workspace_id: WS_A,
          },
        ],
      }),
    );
    saveHistory(storage, scopeA, [userMessage("q"), first, userMessage("q2"), second]);

    const restored = loadHistory(storage, scopeA);
    // restored = [user, answer-a, user, answer-b]
    expect(restored).toHaveLength(4);
    expect(citationForChunk(restored[1], "chunk-b")).toBeNull();
    expect(citationForChunk(restored[1], "chunk-a")?.filename).toBe("alpha.pdf");
    expect(citationForChunk(restored[3], "chunk-a")).toBeNull();
    expect(citationForChunk(restored[3], "chunk-b")?.filename).toBe("beta.pdf");
  });

  it("nothing stored yet restores an empty transcript, not a crash", () => {
    expect(loadHistory(fakeStorage(), scopeA)).toEqual([]);
  });
});

describe("workspace switch re-scopes (0 cross-workspace leaks)", () => {
  it("each workspace reads only its own transcript", () => {
    const storage = fakeStorage();
    saveHistory(storage, { account: ACCOUNT_A, workspace: WS_A }, [
      userMessage("A question"),
      answerMessage(payload()),
    ]);
    saveHistory(storage, { account: ACCOUNT_A, workspace: WS_B }, [userMessage("B question")]);

    expect(loadHistory(storage, { account: ACCOUNT_A, workspace: WS_A }).map((m) => m.content)).toEqual([
      "A question",
      "Alpha answer.",
    ]);
    expect(loadHistory(storage, { account: ACCOUNT_A, workspace: WS_B }).map((m) => m.content)).toEqual([
      "B question",
    ]);
  });

  it("a workspace with no history of its own restores empty", () => {
    const storage = fakeStorage();
    saveHistory(storage, scopeA, [userMessage("only in A")]);
    expect(loadHistory(storage, { account: ACCOUNT_A, workspace: WS_B })).toEqual([]);
  });

  it("an envelope written for another workspace never renders, even if it sits under the right key", () => {
    // Defence in depth: the key is scoped AND the payload repeats the scope,
    // so a copied/tampered payload cannot be replayed into another scope.
    const storage = fakeStorage();
    saveHistory(storage, scopeA, [userMessage("secret of A")]);
    const foreign = storage.raw(historyStorageKey(scopeA)!)!;
    storage.put(historyStorageKey({ account: ACCOUNT_A, workspace: WS_B })!, foreign);

    expect(loadHistory(storage, { account: ACCOUNT_A, workspace: WS_B })).toEqual([]);
  });

  it("clearHistory drops one scope and leaves the other intact", () => {
    const storage = fakeStorage();
    saveHistory(storage, scopeA, [userMessage("A")]);
    saveHistory(storage, { account: ACCOUNT_A, workspace: WS_B }, [userMessage("B")]);

    clearHistory(storage, scopeA);

    expect(loadHistory(storage, scopeA)).toEqual([]);
    expect(loadHistory(storage, { account: ACCOUNT_A, workspace: WS_B })).toHaveLength(1);
  });
});

describe("account scoping", () => {
  it("two accounts on one workspace never read each other's transcript", () => {
    const storage = fakeStorage();
    saveHistory(storage, { account: ACCOUNT_A, workspace: WS_A }, [
      userMessage("A's question"),
    ]);
    expect(loadHistory(storage, { account: ACCOUNT_B, workspace: WS_A })).toEqual([]);

    saveHistory(storage, { account: ACCOUNT_B, workspace: WS_A }, [
      userMessage("B's question"),
    ]);
    expect(loadHistory(storage, { account: ACCOUNT_A, workspace: WS_A }).map((m) => m.content)).toEqual([
      "A's question",
    ]);
    expect(loadHistory(storage, { account: ACCOUNT_B, workspace: WS_A }).map((m) => m.content)).toEqual([
      "B's question",
    ]);
  });

  it("keys differ per account AND per workspace", () => {
    const base = historyStorageKey(scopeA);
    expect(base).toContain(ACCOUNT_A);
    expect(base).toContain(WS_A);
    expect(base).not.toBe(historyStorageKey({ account: ACCOUNT_B, workspace: WS_A }));
    expect(base).not.toBe(historyStorageKey({ account: ACCOUNT_A, workspace: WS_B }));
  });

  it("a scope that cannot be written into a key is never persisted", () => {
    // ":" is the key separator — allowing it would make two different
    // (account, workspace) pairs able to produce the same key.
    expect(historyStorageKey({ account: `evil${":"}${WS_A}`, workspace: WS_A })).toBeNull();
    expect(historyStorageKey({ account: "acct/../other", workspace: WS_A })).toBeNull();
    expect(saveHistory(fakeStorage(), { account: "a:b", workspace: WS_A }, [userMessage("x")])).toBe(
      false,
    );
    expect(loadHistory(fakeStorage(), { account: "a:b", workspace: WS_A })).toEqual([]);
  });
});

describe("validation and failure modes", () => {
  it("corrupt payloads restore empty instead of throwing", () => {
    const storage = fakeStorage();
    storage.put(historyStorageKey(scopeA)!, "{not json[");
    expect(() => loadHistory(storage, scopeA)).not.toThrow();
    expect(loadHistory(storage, scopeA)).toEqual([]);
  });

  it("an unknown schema version is dropped, not guessed at", () => {
    const storage = fakeStorage();
    storage.put(
      historyStorageKey(scopeA)!,
      JSON.stringify({
        version: HISTORY_SCHEMA_VERSION + 1,
        account: ACCOUNT_A,
        workspace: WS_A,
        saved_at: "2026-10-12T00:00:00.000Z",
        messages: [userMessage("future")],
      }),
    );
    expect(loadHistory(storage, scopeA)).toEqual([]);
  });

  it("malformed messages are dropped, well-formed ones survive", () => {
    const good = userMessage("keep me");
    const envelope = {
      version: HISTORY_SCHEMA_VERSION,
      account: ACCOUNT_A,
      workspace: WS_A,
      saved_at: "2026-10-12T00:00:00.000Z",
      messages: [
        good,
        { role: "system", content: "not a turn", claims: [], citations: [] },
        { role: "user", content: 42, claims: [], citations: [] },
        { role: "assistant", content: "bad claims", claims: "x", citations: [] },
        { role: "assistant", content: "bad citations", claims: [], citations: [{}] },
        "just a string",
      ],
    };

    const restored = readEnvelope(envelope, scopeA);
    expect(restored).toHaveLength(1);
    expect(restored[0].content).toBe("keep me");
  });

  it("normalizeMessage keeps typed optional fields and drops wrong-typed ones", () => {
    const turn = normalizeMessage({
      role: "assistant",
      content: "text",
      claims: [],
      citations: [],
      status: "nonsense",
      confidence: { level: "high", basis: "grounded" },
      limitations: ["one"],
      error: false,
    });
    expect(turn).toMatchObject({
      confidence: { level: "high", basis: "grounded", probability: null },
      limitations: ["one"],
      error: false,
    });
    // An unknown status is dropped rather than passed through to the UI.
    expect(turn?.status).toBeUndefined();
    expect(normalizeMessage({ role: "assistant", content: "x", claims: [], citations: "nope" })).toBeNull();
  });

  it("an unusable store never throws (private mode / quota)", () => {
    const throwing: HistoryStorage = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
      removeItem: () => {
        throw new Error("SecurityError");
      },
    };
    expect(loadHistory(throwing, scopeA)).toEqual([]);
    expect(saveHistory(throwing, scopeA, [userMessage("x")])).toBe(false);
    expect(() => clearHistory(throwing, scopeA)).not.toThrow();

    expect(loadHistory(null, scopeA)).toEqual([]);
    expect(saveHistory(null, scopeA, [userMessage("x")])).toBe(false);
    expect(() => clearHistory(null, scopeA)).not.toThrow();
  });

  it("the transcript is bounded before it is written", () => {
    const storage = fakeStorage();
    const many = Array.from({ length: MAX_PERSISTED_MESSAGES + 50 }, (_, i) =>
      userMessage(`turn ${i}`),
    );
    saveHistory(storage, scopeA, many);

    const restored = loadHistory(storage, scopeA);
    expect(restored).toHaveLength(MAX_PERSISTED_MESSAGES);
    expect(restored[restored.length - 1].content).toBe(`turn ${MAX_PERSISTED_MESSAGES + 49}`);
  });
});
