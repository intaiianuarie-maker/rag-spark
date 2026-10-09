"use client";

import { useState } from "react";
import type { Claim, Citation } from "../contracts";
import { CitationChip } from "./citation-chip";

export function ChatWindow({
  streaming,
  messages,
  citations,
  claimCitations: _claimCitations,
  onSend,
  onOpenSource,
}: {
  streaming: boolean;
  messages: { role: "user" | "assistant"; content: string; claims?: Claim[] }[];
  citations: Citation[];
  claimCitations: Record<string, string[]>;
  onSend: (text: string) => void;
  onOpenSource: (c: Citation) => void;
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
                {msg.claims && msg.claims.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {msg.claims.map((claim, ci) => (
                      <div key={ci} className="space-y-1">
                        <p className="text-sm">{claim.text}</p>
                        <div className="flex flex-wrap gap-1.5">
                          {claim.chunk_ids.map((chunkId) => {
                            const cit = citations.find((c) => c.chunk_id === chunkId);
                            if (!cit) return null;
                            return <CitationChip key={chunkId} citation={cit} onOpen={onOpenSource} />;
                          })}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
          {streaming && (
            <div className="rounded-xl bg-neutral-50 p-4 text-neutral-500 dark:bg-neutral-900 dark:text-neutral-400">
              Thinking…
            </div>
          )}
        </div>
      </div>
      <div className="border-t border-neutral-200 px-6 py-4 dark:border-neutral-800">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!input.trim() || streaming) return;
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
            disabled={streaming}
          />
          <button
            type="submit"
            disabled={!input.trim() || streaming}
            className="rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Send
          </button>
        </form>
      </div>
    </div>
  );
}
