/**
 * TASK-M4-002b — unverified claim rendering.
 *
 * A claim whose chunk id(s) cannot be resolved against THIS message's own
 * citations renders a distinct, non-clickable "unverified" marker alongside
 * any resolvable chips. The marker must never look like a source: not a link,
 * not a button, no navigation on click, an explicit accessible label, and a
 * neutral (non-citation-chip) style.
 *
 * Rendered to static markup so the assertions are DOM-free (vitest runs in
 * node, no DOM test harness in this repo).
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { JSX } from "react";

import { ChatWindow, answerMessage, userMessage, type ChatMessage } from "./components/chat-window";
import type { AnswerPayload } from "./contracts";

function payload(overrides: Partial<AnswerPayload> = {}): AnswerPayload {
  return {
    answer: "…",
    status: "answered",
    claims: [],
    citations: [],
    ...overrides,
  };
}

function citation(overrides: { chunk_id: string; filename: string }): { chunk_id: string; document_id: string | null; filename: string | null; page: number | null; section: string | null; span: [number, number] | null; workspace_id: string | null } {
  return {
    chunk_id: overrides.chunk_id,
    document_id: "doc-a",
    filename: overrides.filename,
    page: 1,
    section: null,
    span: null,
    workspace_id: "ws-1",
  };
}

function renderTranscript(messages: ChatMessage[]): string {
  return renderToStaticMarkup(
    <div>
      {messages.map((msg) => (
        <div key={msg.content}>
          {msg.role === "assistant" ? (
            <ChatWindowForTest msg={msg} />
          ) : (
            <p>{msg.content}</p>
          )}
        </div>
      ))}
    </div>,
  );
}

function ChatWindowForTest({ msg }: { msg: ChatMessage }): JSX.Element {
  return (
    <ChatWindow
      pending={false}
      messages={[msg]}
      streamingContent=""
      onSend={() => {}}
      onOpenSource={() => {}}
    />
  );
}

const RESOLVED = citation({ chunk_id: "chunk-a", filename: "alpha.pdf" });

describe("unverified claim rendering", () => {
  it("renders a distinct non-clickable unverified marker for unresolvable chunks", () => {
    const messages: ChatMessage[] = [
      userMessage("Q"),
      answerMessage(
        payload({
          claims: [
            { text: "Fully sourced claim.", chunk_ids: ["chunk-a"] },
            { text: "Fully unverified claim.", chunk_ids: ["chunk-missing"] },
            { text: "Partially sourced claim.", chunk_ids: ["chunk-x", "chunk-y"] },
          ],
          citations: [RESOLVED],
        }),
      ),
    ];

    const html = renderTranscript(messages);

    // Two claims carry unresolvable chunks → two unverified markers.
    const markers = html.match(/aria-label="unverified claim"/g);
    expect(markers).toHaveLength(2);

    // Each marker is a <span>, never an <a> link and never a clickable chip.
    expect(html).not.toContain("<a ");
    const unverifiedSpans = html.match(/<span[^>]*aria-label="unverified claim"[^>]*>/g);
    expect(unverifiedSpans).toHaveLength(2);
    for (const span of unverifiedSpans ?? []) {
      expect(span).not.toContain("href");
      expect(span).not.toContain("onClick");
      expect(span).not.toContain("bg-blue"); // not a citation-chip colour
    }

    // The marker text is present and the neutral style is applied.
    expect(html).toContain("unverified");
    expect(html).toContain("bg-neutral-100");
    expect(html).toContain("text-neutral-500");

    // Resolvable chunks still render their citation chips; the fully unverified
    // claim renders no chip at all. The chip is the only blue element; the
    // unverified marker is neutral, so count the citation-chip colour.
    expect(html.match(/bg-blue-50/g)).toHaveLength(1);
  });

  it("renders no unverified marker when every chunk resolves", () => {
    const messages: ChatMessage[] = [
      answerMessage(
        payload({
          claims: [{ text: "Sourced claim.", chunk_ids: ["chunk-a"] }],
          citations: [RESOLVED],
        }),
      ),
    ];

    const html = renderTranscript(messages);
    expect(html).not.toContain("aria-label=\"unverified claim\"");
    expect(html).toContain("alpha.pdf");
    expect(html.match(/unverified/g)).toBeNull();
  });
});
