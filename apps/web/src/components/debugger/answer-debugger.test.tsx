import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { AnswerDebugger } from "./answer-debugger";
import type { AnswerPayload } from "@/app/ask/contracts";

const base: AnswerPayload = {
  answer: "A",
  status: "answered",
  claims: [{ text: "c", chunk_ids: ["chunk-1"] }],
  citations: [{ chunk_id: "chunk-1", document_id: "d", filename: "f.pdf", page: 1, section: null, span: null, workspace_id: "ws-1" }],
  trace_id: "t-1",
  publication_status: "verified",
  verifier_model: "m",
};

describe("AnswerDebugger", () => {
  it("renders trace_id", () => {
    const html = renderToStaticMarkup(<AnswerDebugger payload={base} />);
    expect(html).toContain("t-1");
  });

  it("discloses absent stages explicitly when no debug_trace", () => {
    const html = renderToStaticMarkup(<AnswerDebugger payload={base} />);
    expect(html).toContain("not reported");
  });

  it("renders debug_trace stages when present", () => {
    const p = {
      ...base,
      debug_trace: {
        version: 1,
        query: { sha256: "abc", characters: 5 },
        rewrite: { status: "not_reported" },
        filters: { workspace_ids: ["ws-1"], include_public: false, mode: "hybrid" },
        retrieval: { status: "ok", latency_ms: 12, candidate_scope: "returned_context_only", items: [{ chunk_id: "chunk-1", document_id: "d", workspace_id: "ws-1", score: 0.9, scores: { dense: 0.8 } }] },
        context: { chunk_ids: ["chunk-1"], source_text_included: false },
        generation: { status: "ok", model: "gpt-4o-mini", prompt_template_sha256: "sha", latency_ms: 100, prompt_tokens: null, completion_tokens: null, usage_status: "not_reported" },
        verification: { phase: "parse_verify_publish", status: "verified", latency_ms: 5, usage_status: "not_reported" },
        total_latency_ms: 120,
      },
    } as unknown as AnswerPayload;
    const html = renderToStaticMarkup(<AnswerDebugger payload={p} />);
    expect(html).toContain("abc");
    expect(html).toContain("returned_context_only");
    expect(html).toContain("chunk-1");
    expect(html).toContain("gpt-4o-mini");
    expect(html).toContain("parse_verify_publish");
    expect(html).toContain("120");
  });

  it("hides source text by default (privacy)", () => {
    const p = { ...base, citations: [{ ...base.citations[0], content: "secret text" }] } as unknown as AnswerPayload;
    const html = renderToStaticMarkup(<AnswerDebugger payload={p} />);
    expect(html).not.toContain("secret text");
  });

  it("shows source text only when opted in", () => {
    const p = { ...base, citations: [{ ...base.citations[0], content: "secret text" }] } as unknown as AnswerPayload;
    const html = renderToStaticMarkup(<AnswerDebugger payload={p} showSourceText />);
    expect(html).toContain("secret text");
  });

  it("renders nothing for null payload", () => {
    const html = renderToStaticMarkup(<AnswerDebugger payload={null} />);
    expect(html).toBe("");
  });
});
