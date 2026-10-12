"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { useWorkspaces } from "@/components/workspace/workspace-provider";
import { askQuestion, type AnswerPayload } from "./contracts";
import {
  ChatWindow,
  answerMessage,
  failureMessage,
  userMessage,
  type ChatMessage,
  type MessageCitation,
} from "./components/chat-window";
import { SourcePanel } from "./components/source-panel";
import { ModelSelector } from "./components/model-selector";
import {
  browserStorage,
  loadHistory,
  saveHistory,
  type HistoryScope,
  type HistoryStorage,
} from "./history";

const MODEL_OPTIONS = [
  { id: "gpt-4o-mini", label: "gpt-4o-mini" },
  { id: "gpt-4o", label: "gpt-4o" },
];

/**
 * Ask screen (T-V1-032 — per-message citations; T-V1-033 — persisted,
 * scope-respecting history, RP-13/QF-13).
 *
 * There is deliberately NO shared "latest answer" slot: every turn is
 * appended to the transcript with its own citations, confidence, limitations
 * and status frozen on that message (see `answerMessage`). Opening a source
 * passes the clicked citation itself to the panel, which fetches that
 * citation's canonical passage — so answer #1 keeps its own evidence after
 * answer #2 arrives, and after a refresh.
 *
 * History (T-V1-033):
 *  - the transcript is PERSISTED through `./history` (durable key/value
 *    store), not held in component state alone — a refresh restores it;
 *  - it is scoped to the signed-in account AND the active workspace. The
 *    scope key is `account::workspace`; the page renders a transcript only
 *    while the current scope matches the one it was loaded for, so switching
 *    workspace immediately re-scopes (no leftover turns, no open source panel
 *    from the previous workspace);
 *  - when the account scope cannot be established (sign-in state unreadable),
 *    the transcript stays in memory for the session and NOTHING is written.
 *
 * Answer delivery is one request → one response. The in-flight indicator
 * reports an unanswered request only; this screen makes no claim about
 * incremental token delivery.
 */
export default function AskPage() {
  const { active } = useWorkspaces();
  const [pending, setPending] = useState(false);
  const [accountScope, setAccountScope] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<{ key: string; messages: ChatMessage[] }>({
    key: "",
    messages: [],
  });
  const [selectedSource, setSelectedSource] = useState<MessageCitation | null>(null);
  const [selectedModel, setSelectedModel] = useState("gpt-4o-mini");
  const storageRef = useRef<HistoryStorage | null | undefined>(undefined);

  const workspaceId = active?.id ?? null;
  /** `account::workspace`, or "" while the scope is unknown (no persistence). */
  const scopeKey =
    accountScope !== null && workspaceId !== null ? `${accountScope}::${workspaceId}` : "";

  const storage = useCallback((): HistoryStorage | null => {
    if (storageRef.current === undefined) storageRef.current = browserStorage();
    return storageRef.current;
  }, []);

  // 1. Which account is signed in on this browser (server reads the httpOnly
  //    cookie — the page never sees a token). Resolved once per mount.
  useEffect(() => {
    let cancelled = false;
    fetch("/api/account-scope", { cache: "no-store", headers: { accept: "application/json" } })
      .then((response) => (response.ok ? response.json() : null))
      .then((body: unknown) => {
        if (cancelled) return;
        const record = typeof body === "object" && body !== null ? body : null;
        const value = record === null ? null : (record as { scope?: unknown }).scope;
        setAccountScope(typeof value === "string" && value.length > 0 ? value : null);
      })
      .catch(() => {
        if (!cancelled) setAccountScope(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 2. Load the transcript for the CURRENT scope, on every scope change.
  //    Declared before the save effect so hydration wins the same commit.
  //    Deps are primitives only: `scope` is rebuilt each render.
  useEffect(() => {
    if (accountScope === null || workspaceId === null) {
      setTranscript({ key: "", messages: [] });
      return;
    }
    setSelectedSource(null); // the open source belongs to the previous scope
    const currentScope: HistoryScope = { account: accountScope, workspace: workspaceId };
    setTranscript({
      key: `${accountScope}::${workspaceId}`,
      messages: loadHistory(storage(), currentScope),
    });
  }, [accountScope, workspaceId, storage]);

  // 3. Persist after every change — only when the transcript already belongs
  //    to the current scope, so a switch can never write one workspace's
  //    turns under another workspace's key.
  useEffect(() => {
    if (accountScope === null || workspaceId === null) return;
    const currentScope: HistoryScope = { account: accountScope, workspace: workspaceId };
    if (transcript.key !== `${accountScope}::${workspaceId}`) return;
    saveHistory(storage(), currentScope, transcript.messages);
  }, [accountScope, workspaceId, transcript, storage]);

  const handleSend = useCallback(
    async (query: string) => {
      if (workspaceId === null) return;
      setPending(true);
      setSelectedSource(null);

      const accountAtSend = accountScope;
      const workspaceAtSend = workspaceId;
      const scopeKeyAtSend = scopeKey;
      const storageAtSend = storage();
      // Functional update so the base transcript is the one for THIS scope:
      // if hydration has not landed yet, read it here instead of starting
      // from an empty list (which would then overwrite stored history).
      setTranscript((current) => {
        if (accountAtSend === null) return { key: "", messages: [...current.messages, userMessage(query)] };
        const scopeAtSend: HistoryScope = { account: accountAtSend, workspace: workspaceAtSend };
        const base =
          current.key === scopeKeyAtSend
            ? current.messages
            : loadHistory(storageAtSend, scopeAtSend);
        return { key: scopeKeyAtSend, messages: [...base, userMessage(query)] };
      });

      try {
        const result: AnswerPayload = await askQuestion(workspaceAtSend, query, selectedModel);
        setTranscript((current) =>
          current.key === scopeKeyAtSend
            ? { ...current, messages: [...current.messages, answerMessage(result)] }
            : current,
        );
      } catch (error: unknown) {
        setTranscript((current) =>
          current.key === scopeKeyAtSend
            ? { ...current, messages: [...current.messages, failureMessage(error)] }
            : current,
        );
      } finally {
        setPending(false);
      }
    },
    [accountScope, workspaceId, scopeKey, selectedModel, storage],
  );

  const messages = transcript.key === scopeKey ? transcript.messages : [];

  return (
    <main className="flex min-h-[calc(100vh-160px)] flex-col md:flex-row">
      <div className="flex-1 flex flex-col">
        <div className="flex items-center justify-between px-6 py-3 border-b border-neutral-200 dark:border-neutral-800 md:px-8 md:py-4">
          <h1 className="text-lg font-semibold text-neutral-900 dark:text-neutral-100">Ask</h1>
          <ModelSelector
            value={selectedModel}
            onChange={setSelectedModel}
            options={MODEL_OPTIONS}
          />
        </div>
        <ChatWindow
          pending={pending}
          messages={messages}
          onSend={handleSend}
          onOpenSource={setSelectedSource}
        />
      </div>
      <SourcePanel
        citation={selectedSource}
        workspaceId={selectedSource?.workspace_id ?? active?.id ?? null}
        onClose={() => setSelectedSource(null)}
      />
    </main>
  );
}
