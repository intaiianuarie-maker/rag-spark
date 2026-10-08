"use client";

import { useState, useCallback } from "react";
import { useWorkspaces } from "@/components/workspace/workspace-provider";
import { askQuestion, type AnswerPayload } from "./contracts";
import { ChatWindow } from "./components/chat-window";
import { SourcePanel } from "./components/source-panel";
import { ModelSelector } from "./components/model-selector";
import type { Citation } from "./contracts";

const MODEL_OPTIONS = [
  { id: "gpt-4o-mini", label: "gpt-4o-mini" },
  { id: "gpt-4o", label: "gpt-4o" },
];

export default function AskPage() {
  const { active } = useWorkspaces();
  const [streaming, setStreaming] = useState(false);
  const [messages, setMessages] = useState<
    { role: "user" | "assistant"; content: string; claims?: { text: string; chunk_ids: string[] }[] }[]
  >([]);
  const [answer, setAnswer] = useState<AnswerPayload | null>(null);
  const [selectedSource, setSelectedSource] = useState<Citation | null>(null);
  const [selectedModel, setSelectedModel] = useState("gpt-4o-mini");

  const handleSend = useCallback(
    async (query: string) => {
      if (!active?.id) return;
      setStreaming(true);
      setAnswer(null);
      setSelectedSource(null);
      setMessages((prev) => [...prev, { role: "user", content: query }]);

      try {
        const result = await askQuestion(active.id, query, selectedModel);
        setAnswer(result);
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: result.answer,
            claims: result.claims,
          },
        ]);
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Something went wrong.";
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: `Error: ${msg}` },
        ]);
      } finally {
        setStreaming(false);
      }
    },
    [active?.id, selectedModel],
  );

  const citations = answer?.citations ?? [];
  const claimCitations: Record<string, string[]> = {};
  if (answer) {
    answer.claims.forEach((claim) => {
      claimCitations[claim.text] = claim.chunk_ids;
    });
  }

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
          citations={citations}
          claimCitations={claimCitations}
          onSend={handleSend}
          onOpenSource={setSelectedSource}
        />
      </div>
      <SourcePanel citation={selectedSource} onClose={() => setSelectedSource(null)} />
    </main>
  );
}
