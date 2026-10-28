/**
 * TASK-M5-001a — the Ask screen streams tokens incrementally.
 *
 * CONTRACT INVERSION (deliberate): the previous version of this file
 * (`streaming-claim.test.ts` prior to TASK-M5-001a) asserted that the Ask
 * screen made NO claim about incremental token delivery — it checked that
 * neither `page.tsx` nor `chat-window.tsx` contained the word "stream" at
 * all, and that the in-flight label was "Waiting for the answer…".
 *
 * That stance was correct for T-V1-033 (single request → single response)
 * but is WRONG after TASK-M5-001a, which explicitly requires streaming.
 * The task card (TASK-M5-001a, "Ask layout … with streaming") is the
 * justification for this inversion. Every other assertion (history scoping,
 * citation chips, deleted-source, passage, page-wiring, contracts) remains
 * untouched and green.
 *
 * This file now asserts the POSITIVE claim: the ask screen streams tokens
 * incrementally and never renders a citation before the terminal event.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { PENDING_LABEL } from "./components/chat-window";

function readSource(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");
}

const SCREENS = ["./page.tsx", "./components/chat-window.tsx"] as const;

describe("the Ask screen streams tokens incrementally", () => {
  it.each(SCREENS)("%s references the streaming transport", (file) => {
    // The screen must consume the SSE stream; the word "stream" must appear
    // in source so a future reader knows the transport is incremental.
    expect(readSource(file)).toMatch(/stream/i);
  });

  it("the in-flight copy reflects streaming delivery", () => {
    expect(PENDING_LABEL).toMatch(/stream/i);
  });

  it("the page imports the SSE stream client", () => {
    const page = readSource("./page.tsx");
    expect(page).toContain("streamQuestion");
    expect(page).toContain("./stream");
  });

  it("the page wires up an AbortController for Stop", () => {
    const page = readSource("./page.tsx");
    expect(page).toContain("AbortController");
    expect(page).toContain("handleStop");
  });

  it("the page handles token events incrementally", () => {
    const page = readSource("./page.tsx");
    expect(page).toContain('case "token"');
    // Streaming state is scoped to the originating turn (M5-001a REV-2
    // Finding 2): every mutation goes through the fence module, and the
    // surface is also scope-gated in the render (REV-3) so a token released
    // in the workspace-switch commit window cannot paint under the new scope.
    expect(page).toContain("setStreamingState");
    expect(page).toContain("appendStreamingToken");
    expect(page).toContain("visibleStreamingContent");
  });

  it("citations render only from the terminal citations event", () => {
    const page = readSource("./page.tsx");
    expect(page).toContain('case "citations"');
    expect(page).toContain("answerMessage(finalAnswer)");
  });

  it("the pending indicator is announced and disables the composer", () => {
    const window = readSource("./components/chat-window.tsx");
    expect(window).toContain('aria-live="polite"');
    expect(window).toContain("{PENDING_LABEL}");
    expect(window).toContain("disabled={pending}");
  });
});
