/**
 * T-V1-033 — the panel's explicit "source deleted" state (RP-13/QF-13).
 *
 * A citation whose document is gone renders a labeled `deleted` state. It
 * never renders silently, never borrows another message's text, and the only
 * body it may show next to the notice is THIS citation's own recorded text.
 */
import { describe, expect, it } from "vitest";

import {
  SOURCE_DELETED_CODE,
  buildPassageUrl,
  resolvePassage,
} from "./components/source-panel";
import type { MessageCitation } from "./components/chat-window";

function citation(overrides: Partial<MessageCitation> = {}): MessageCitation {
  return {
    chunk_id: "chunk-a",
    document_id: "doc-a",
    filename: "alpha.pdf",
    page: 1,
    section: null,
    span: null,
    workspace_id: "ws-1",
    content: null,
    ...overrides,
  };
}

const deleted = (message = "Gone from the workspace.") => ({
  ok: false as const,
  code: SOURCE_DELETED_CODE,
  message,
});

describe("resolvePassage on a deleted source", () => {
  it("renders the explicit deleted state even when the citation has text", () => {
    const view = resolvePassage(deleted(), citation({ content: "text recorded with the answer" }));

    expect(view).toEqual({
      state: "deleted",
      message: "Gone from the workspace.",
      embedded: "text recorded with the answer",
    });
    // The point of the ticket: no silent fallback into the "ready" state.
    expect(view.state).not.toBe("ready");
  });

  it("renders the explicit deleted state with no text at all", () => {
    expect(resolvePassage(deleted(), citation())).toEqual({
      state: "deleted",
      message: "Gone from the workspace.",
      embedded: null,
    });
  });

  it("only ever shows the clicked citation's own recorded text", () => {
    const mine = citation({ content: "mine" });
    const theirs = citation({ chunk_id: "chunk-b", filename: "beta.pdf", content: "theirs" });

    expect(resolvePassage(deleted(), mine)).toMatchObject({ embedded: "mine" });
    expect(resolvePassage(deleted(), theirs)).toMatchObject({ embedded: "theirs" });
    expect(resolvePassage(deleted(), mine)).not.toMatchObject({ embedded: "theirs" });
  });

  it("leaves the T-V1-032 behaviour for non-deletion failures untouched", () => {
    const view = resolvePassage(
      { ok: false, code: "passage_unavailable", message: "not yet" },
      citation({ content: "recorded" }),
    );
    expect(view).toMatchObject({ state: "ready", origin: "answer_embedded" });

    const noText = resolvePassage(
      { ok: false, code: "passage_unavailable", message: "not yet" },
      citation(),
    );
    expect(noText).toMatchObject({ state: "error", code: "passage_unavailable" });
  });
});

describe("buildPassageUrl carries the document ref", () => {
  it("adds document_id only when there is one", () => {
    expect(buildPassageUrl("ws-1", "chunk-a", "doc-a")).toBe(
      "/api/passage?workspace_id=ws-1&chunk_id=chunk-a&document_id=doc-a",
    );
    expect(buildPassageUrl("ws-1", "chunk-a", null)).toBe(
      "/api/passage?workspace_id=ws-1&chunk_id=chunk-a",
    );
    expect(buildPassageUrl("ws-1", "chunk-a")).toBe(
      "/api/passage?workspace_id=ws-1&chunk_id=chunk-a",
    );
  });

  it("encodes the document ref like every other ref", () => {
    expect(buildPassageUrl("ws-1", "chunk-a", "doc/../x")).toBe(
      `/api/passage?${new URLSearchParams({
        workspace_id: "ws-1",
        chunk_id: "chunk-a",
        document_id: "doc/../x",
      })}`,
    );
  });
});
