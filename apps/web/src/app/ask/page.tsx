"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { useWorkspaces } from "@/components/workspace/workspace-provider";
import { auth } from "@/lib/auth-client";
import { listCollections } from "@/app/knowledge/contracts";
import { type AnswerPayload } from "./contracts";
import {
  ChatWindow,
  answerMessage,
  failureMessage,
  userMessage,
  type ChatMessage,
  type MessageCitation,
} from "./components/chat-window";
import { SourcePanel, parseSourceRef, serializeSourceRef } from "./components/source-panel";
import { ModelSelector } from "./components/model-selector";
import {
  browserStorage,
  loadHistory,
  saveHistory,
  type HistoryScope,
  type HistoryStorage,
} from "./history";
import { streamQuestion, type FeedbackPayload } from "./stream";
import {
  appendStreamingToken,
  beginStreamingTurn,
  endStreamingTurn,
  isActiveTurn,
  type StreamingScopeState,
} from "./streaming-scope";

const WORKSPACE_DEFAULT_OPTION = { id: "", label: "Workspace default" };

/**
 * Ask screen (T-V1-032 — per-message citations; T-V1-033 — persisted,
 * scope-respecting history; TASK-M5-001a — streaming with stop/regenerate/
 * copy/feedback).
 *
 * Answer delivery is now STREAMING: tokens arrive incrementally via SSE,
 * citations only render from the terminal `citations` event.
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
  const [selectorConfig, setSelectorConfig] = useState<{
    scope: string;
    selected: string;
    models: { id: string; label: string }[];
    collection: { id: string; name: string } | null;
    error: string | null;
  } | null>(null);
  const [selectorScope, setSelectorScope] = useState("");
  /**
   * Streaming state scoped to the TURN that produced it (M5-001a REV-2
   * Finding 2). A global `streamingContent` string let a workspace switch
   * mid-generation deliver A's tokens onto B's last assistant message; the
   * turn id + originating scope key are pinned with the content, and EVERY
   * mutation goes through the fence in `streaming-scope.ts` (REV-3) so a
   * late token — and equally a late turn COMPLETION — for a superseded turn
   * can no longer mutate or clear the new scope's surface.
   */
  const [streamingState, setStreamingState] = useState<StreamingScopeState | null>(null);
  /** Last URL hash the deep-link effect applied, so we don't re-open on every render. */
  const lastHashRef = useRef<string | null>(null);
  const storageRef = useRef<HistoryStorage | null | undefined>(undefined);
  const abortControllerRef = useRef<AbortController | null>(null);
  /** The turn currently being streamed, for abort-on-scope-change. */
  const activeTurnRef = useRef<{ turnId: string; scopeKey: string } | null>(null);
  /**
   * Every in-flight turn's abort controller, keyed by turn id (REV-3).
   * A scope can legitimately hold MORE THAN ONE controller: a regenerate, or
   * a turn started during the NEW scope's own commit window. The
   * scope-change teardown must end only the OLD scope's streams — taking the
   * new scope's turn with it would kill a legitimate in-flight request (the
   * commit-window ordering this fence exists for).
   */
  const turnControllersRef = useRef<
    Map<string, { scopeKey: string; controller: AbortController }>
  >(new Map());

  const workspaceId = active?.id ?? null;
  /** `account::workspace`, or "" while the scope is unknown (no persistence). */
  const scopeKey =
    accountScope !== null && workspaceId !== null ? `${accountScope}::${workspaceId}` : "";

  // Discard selection during the scope-change render, before the next commit.
  // Clearing on each transition also prevents A -> B -> A from reviving A's
  // previous override while its new metadata request is pending.
  if (selectorScope !== scopeKey) {
    setSelectorScope(scopeKey);
    setSelectorConfig(null);
  }

  // Fence the render and outgoing override before effects run on scope changes.
  const visibleSelectors = selectorScope === scopeKey && selectorConfig?.scope === scopeKey
    ? selectorConfig : null;
  const selectedModel = visibleSelectors?.selected ?? "";
  const modelOptions = visibleSelectors?.models ?? [WORKSPACE_DEFAULT_OPTION];
  const collection = visibleSelectors?.collection ?? null;
  const setSelectedModel = useCallback((selected: string) => {
    setSelectorConfig((previous) => previous?.scope === scopeKey &&
      previous.models.some((option) => option.id === selected)
      ? { ...previous, selected } : previous);
  }, [scopeKey]);

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

  // 2b. Abort an in-flight stream on scope change or unmount (M5-001a REV-2
  // Finding 2). A generation started in workspace A must not keep running after
  // the user switches to B — its tokens are now scoped to a superseded turn and
  // would be dropped by the turn-id guard, but the network/stream should still
  // be torn down so no background work outlives the page's scope.
  //
  // REV-3: the teardown is scoped to the OLD scope's controllers only. A turn
  // that began during the NEW scope's own commit window (its scopeKey differs
  // from this effect's) survives — aborting it would kill a legitimate
  // in-flight turn, the same commit-window class as the fence itself. Shared
  // flags are NOT cleared here: the superseded turn's own `finally` clears
  // them (it is still the owner), and a new-scope turn's completion cannot
  // clear them either (that is the turn gate).
  useEffect(() => {
    // The Map OBJECT is stable for the component's lifetime — only its
    // entries change. Capturing it here (React lint's ref-in-cleanup rule)
    // keeps the CONTENTS live at cleanup time, which is exactly what the
    // scope-classification needs: entries registered after this effect ran
    // (e.g. a turn started during the new scope's own commit window) are
    // still classified correctly.
    const controllers = turnControllersRef.current;
    return () => {
      for (const [turnId, entry] of controllers) {
        if (entry.scopeKey === scopeKey) {
          entry.controller.abort();
          controllers.delete(turnId);
        }
      }
    };
  }, [scopeKey]);

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
    lastHashRef.current = null;
    // A new question invalidates any pasted source deep-link.
    window.location.hash = "";

    const accountAtSend = accountScope;
    const workspaceAtSend = workspaceId;
    const scopeKeyAtSend = scopeKey;
    const storageAtSend = storage();
    // Stable id for THIS turn — streaming state is keyed by it, so a scope
    // switch mid-generation can never deliver this turn's tokens onto the
    // next scope's transcript (M5-001a REV-2 Finding 2).
    const turnId = `${scopeKeyAtSend}::${Date.now().toString(36)}::${Math.random().toString(36).slice(2, 8)}`;
    activeTurnRef.current = { turnId, scopeKey: scopeKeyAtSend };
    setStreamingState(beginStreamingTurn(turnId, scopeKeyAtSend));

      // Functional update so the base transcript is the one for THIS scope:
      setTranscript((current) => {
        if (accountAtSend === null) return { key: "", messages: [...current.messages, userMessage(query)] };
        const scopeAtSend: HistoryScope = { account: accountAtSend, workspace: workspaceAtSend };
        const base =
          current.key === scopeKeyAtSend
            ? current.messages
            : loadHistory(storageAtSend, scopeAtSend);
        return { key: scopeKeyAtSend, messages: [...base, userMessage(query)] };
      });

      // Add a placeholder assistant message that will be filled by streaming
      setTranscript((current) => {
        if (current.key !== scopeKeyAtSend) return current;
        return { ...current, messages: [...current.messages, {
          role: "assistant" as const,
          content: "",
          claims: [],
          citations: [],
          status: undefined,
          error: false,
        }] };
      });

      const abortController = new AbortController();
      abortControllerRef.current = abortController;
      // Registered so the scope-change teardown can find THIS turn's stream
      // by scope (and only ever abort the old scope's) — see 2b.
      turnControllersRef.current.set(turnId, {
        scopeKey: scopeKeyAtSend,
        controller: abortController,
      });

      let accumulatedText = "";

      try {
        const stream = streamQuestion(workspaceAtSend, query, selectedModel, abortController.signal, turnId);
        let finalAnswer: AnswerPayload | null = null;

        for await (const event of stream) {
          if (abortController.signal.aborted) break;

          switch (event.type) {
            case "token":
              // Only deliver tokens to the turn they belong to. A late token
              // for a superseded turn (user switched workspace mid-generation)
              // is ignored — the new scope's transcript is never mutated.
              if (event.turnId !== turnId) continue;
              accumulatedText += event.text;
              setStreamingState((current) =>
                appendStreamingToken(current, turnId, event.text),
              );
              break;
            case "citations":
              if (event.turnId !== turnId) continue;
              finalAnswer = event.answer;
              break;
            case "error":
              if (event.turnId !== turnId) continue;
              // Mark as error
              setTranscript((current) => {
                if (current.key !== scopeKeyAtSend) return current;
                const messages = [...current.messages];
                const last = messages[messages.length - 1];
                if (last?.role === "assistant") {
                  messages[messages.length - 1] = {
                    ...last,
                    content: accumulatedText || `Error: ${event.message}`,
                    error: true,
                  };
                }
                return { ...current, messages };
              });
              // Release only the surface THIS turn still owns, and clear the
              // pending flag only while this turn is the active one — a
              // superseded turn's error must not clear the new scope's
              // in-flight turn (REV-3).
              setStreamingState((current) => endStreamingTurn(current, turnId));
              if (isActiveTurn(activeTurnRef.current, turnId)) {
                setPending(false);
              }
              return;
          }
        }

        // Finalize the assistant message
        setTranscript((current) => {
          if (current.key !== scopeKeyAtSend) return current;
          const messages = [...current.messages];
          const last = messages[messages.length - 1];
          if (last?.role === "assistant") {
            if (finalAnswer) {
              messages[messages.length - 1] = answerMessage(finalAnswer);
            } else if (accumulatedText) {
              // Streamed but no citations event (interrupted)
              messages[messages.length - 1] = {
                ...last,
                content: accumulatedText,
                interrupted: true,
              };
            }
          }
          return { ...current, messages };
        });
      } catch (error: unknown) {
        if (abortController.signal.aborted) {
          // User stopped — mark as interrupted
          setTranscript((current) => {
            if (current.key !== scopeKeyAtSend) return current;
            const messages = [...current.messages];
            const last = messages[messages.length - 1];
            if (last?.role === "assistant") {
              messages[messages.length - 1] = {
                ...last,
                content: accumulatedText || last.content,
                interrupted: true,
              };
            }
            return { ...current, messages };
          });
        } else {
          setTranscript((current) => {
            if (current.key !== scopeKeyAtSend) return current;
            const messages = [...current.messages];
            const last = messages[messages.length - 1];
            if (last?.role === "assistant") {
              messages[messages.length - 1] = failureMessage(error);
            }
            return { ...current, messages };
          });
        }
      } finally {
        // REV-3: the exit path is turn-gated as well. After an
        // abort-on-scope-change the superseded turn's completion must not
        // clear the NEW scope's streaming surface, its pending indicator, or
        // its abort controller.
        turnControllersRef.current.delete(turnId);
        setStreamingState((current) => endStreamingTurn(current, turnId));
        if (isActiveTurn(activeTurnRef.current, turnId)) {
          setPending(false);
          abortControllerRef.current = null;
          activeTurnRef.current = null;
        }
      }
    },
    [accountScope, workspaceId, scopeKey, selectedModel, storage],
  );

  const handleStop = useCallback(() => {
    abortControllerRef.current?.abort();
  }, []);

  const handleRegenerate = useCallback(
    (query: string) => {
      // Remove the last assistant message and re-ask
      setTranscript((current) => {
        if (current.key !== scopeKey) return current;
        const messages = [...current.messages];
        // Remove trailing assistant message
        if (messages.length > 0 && messages[messages.length - 1].role === "assistant") {
          messages.pop();
        }
        return { ...current, messages };
      });
      handleSend(query);
    },
    [scopeKey, handleSend],
  );

  const handleCopy = useCallback((text: string) => {
    navigator.clipboard?.writeText(text).catch(() => undefined);
  }, []);

  const handleFeedback = useCallback((turnId: string, rating: "positive" | "negative" | "none", comment?: string) => {
    const payload: FeedbackPayload = {
      turnId,
      rating,
      comment,
      answeredAt: new Date().toISOString(),
    };
    // No backend endpoint yet — persist locally via browserStorage
    const storageAtSend = storage();
    if (storageAtSend) {
      try {
        const key = `kios_ask_feedback:${turnId}`;
        storageAtSend.setItem(key, JSON.stringify(payload));
      } catch {
        // Storage unavailable — feedback is ephemeral
      }
    }
  }, [storage]);

  const messages = transcript.key === scopeKey ? transcript.messages : [];

  // Metadata shares the transcript's account/workspace authorization scope.
  const configEpochRef = useRef(0);
  useEffect(() => {
    const epoch = ++configEpochRef.current;
    const ws = workspaceId;
    if (!ws || !scopeKey) return;
    type Config = {
      preferred_model?: string | null;
      override_model?: string | null;
      extra_models?: Array<{ id?: unknown; model_id?: unknown }> | null;
    };
    void Promise.allSettled([
      auth.authFetch<Config[]>(`/v1/workspaces/${ws}/model-configs`, {}),
      listCollections(ws),
    ]).then(([configs, collections]) => {
      if (epoch !== configEpochRef.current) return;
      const ids = new Set<string>();
      if (configs.status === "fulfilled") {
        for (const config of configs.value) {
          for (const id of [config.preferred_model, config.override_model,
            ...(config.extra_models ?? []).map((extra) => extra.id ?? extra.model_id)]) {
            if (typeof id === "string" && id.trim()) ids.add(id);
          }
        }
      }
      const implicit = collections.status === "fulfilled" && Array.isArray(collections.value)
        ? collections.value.find((item) => item.id === ws) ?? null : null;
      setSelectorConfig({
        scope: scopeKey,
        selected: "",
        models: [WORKSPACE_DEFAULT_OPTION, ...Array.from(ids, (id) => ({ id, label: id }))],
        collection: implicit,
        error: configs.status === "rejected"
          ? "Configured models unavailable. Workspace default remains available."
          : implicit === null ? "Collection unavailable." : null,
      });
    });
    return () => { configEpochRef.current += 1; };
  }, [workspaceId, scopeKey]);

  // The streaming surface is scope-gated in the RENDER too (REV-3). The
  // mutation fence drops a superseded turn's tokens once the new scope owns
  // the surface, but between the workspace switch render and the abort
  // teardown the surface still belongs to the old turn — a token released in
  // that commit window would otherwise paint under the new scope's history.
  const visibleStreamingContent =
    streamingState !== null && streamingState.scopeKey === scopeKey
      ? streamingState.content
      : "";

  // A pasted source deep-link opens the right message's panel on load and
  // scrolls that message into view. Garbage / another-workspace hashes are
  // refused by parseSourceRef and leave the panel closed. Re-runs on
  // transcript hydration, workspace change AND in-page hash navigation
  // (back/forward/paste) so a deep-link always resolves.
  const applyDeepLink = useCallback(() => {
    if (workspaceId === null) return;
    const hash = window.location.hash;
    const ref = parseSourceRef(hash, workspaceId);
    if (!ref) {
      // Foreign / malformed / missing hash: close any open panel instead of
      // leaving a stale source visible. Hydration/re-navigation re-runs this.
      lastHashRef.current = null;
      setSelectedSource(null);
      return;
    }
    const msg = messages[ref.messageIndex];
    const cit = msg?.citations.find((c) => c.chunk_id === ref.chunkId);
    // Enforce the source's workspace against the current one; a mismatched or
    // not-yet-hydrated citation closes the panel rather than retaining an
    // unrelated one. A later hydration re-runs this and reopens if it resolves.
    if (!cit || (cit.workspace_id != null && cit.workspace_id !== workspaceId)) {
      setSelectedSource(null);
      return;
    }
    if (lastHashRef.current === hash) return;
    setSelectedSource(cit);
    const el = document.getElementById(`message-${ref.messageIndex}`);
    el?.scrollIntoView({ block: "center", behavior: "auto" });
    lastHashRef.current = hash;
  }, [messages, workspaceId]);

  useEffect(() => {
    applyDeepLink();
  }, [applyDeepLink]);

  useEffect(() => {
    const onHashChange = () => applyDeepLink();
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [applyDeepLink]);

  return (
    <main className="flex min-h-[calc(100vh-160px)] flex-col md:flex-row">
      <div className="flex-1 flex flex-col">
        <div className="flex items-center justify-between px-6 py-3 border-b border-neutral-200 dark:border-neutral-800 md:px-8 md:py-4">
          <h1 className="text-lg font-semibold text-neutral-900 dark:text-neutral-100">Ask</h1>
          <ModelSelector
            key={scopeKey}
            value={selectedModel}
            onChange={setSelectedModel}
            options={modelOptions}
          />
          <label className="text-xs text-neutral-500">
            Collection
            <select aria-label="Collection" className="ml-2 rounded border px-2 py-1"
              value={collection?.id ?? ""} disabled={!collection} onChange={() => {}}>
              <option value={collection?.id ?? ""}>
                {collection?.name ?? "Workspace collection"}
              </option>
            </select>
          </label>
          {visibleSelectors?.error && <p role="status" className="text-xs text-amber-700">{visibleSelectors.error}</p>}
        </div>
        <ChatWindow
          pending={pending}
          messages={messages}
          streamingContent={visibleStreamingContent}
          onSend={handleSend}
          onStop={handleStop}
          onOpenSource={useCallback(
            (citation, messageIndex) => {
              setSelectedSource(citation);
              if (workspaceId !== null) {
                window.location.hash = serializeSourceRef({
                  workspaceId,
                  messageIndex,
                  chunkId: citation.chunk_id,
                });
              }
            },
            [workspaceId],
          )}
          onRegenerate={handleRegenerate}
          onCopy={handleCopy}
          onFeedback={handleFeedback}
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
