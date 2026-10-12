"use client";

import { useState, useCallback } from "react";
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

const MODEL_OPTIONS = [
  { id: "gpt-4o-mini", label: "gpt-4o-mini" },
  { id: "gpt-4o", label: "gpt-4o" },
];

/**
 * Ask screen (T-V1-032 — RP-13/QF-13).
 *
 * There is deliberately NO shared "latest answer" slot any more: every turn
 * is appended to `messages` with its own citations, confidence, limitations
 * and status frozen on that message (see `answerMessage`). Opening a source
 * passes the clicked citation itself to the panel, which fetches that
 * citation's canonical passage — so answer #1 keeps its own evidence after
 * answer #2 arrives.
 */
export default function AskPage() {
  const { active } = useWorkspaces();
  const [streaming, setStreaming] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [selectedSource, setSelectedSource] = useState<MessageCitation | null>(null);
  const [selectedModel, setSelectedModel] = useState("gpt-4o-mini");

  const handleSend = useCallback(
    async (query: string) => {
      if (!active?.id) return;
      setStreaming(true);
      setSelectedSource(null);
      setMessages((prev) => [...prev, userMessage(query)]);

      try {
        const result: AnswerPayload = await askQuestion(active.id, query, selectedModel);
        setMessages((prev) => [...prev, answerMessage(result)]);
      } catch (err) {
        setMessages((prev) => [...prev, failureMessage(err)]);
      } finally {
        setStreaming(false);
      }
    },
    [active, selectedModel],
  );

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
          streaming={streaming}
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
