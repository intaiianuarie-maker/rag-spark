/**
 * T-V1-032 — per-message citations (RP-13 / QF-13).
 *
 * Success criterion: "two-answer independence test green (different docs,
 * both links correct)" and metric "cross-message citation mixups: 0".
 *
 * These tests drive the pure message builders used by `page.tsx`, so they pin
 * the property the ticket is actually about: an answer's citations,
 * confidence, limitations and status are stored WITH that message and are
 * never read back from a "latest answer" slot.
 */
import { describe, expect, it } from "vitest";

import {
  answerMessage,
  citationForChunk,
  failureMessage,
  userMessage,
  type MessageCitation,
} from "./components/chat-window";
import type { AnswerPayload } from "./contracts";

function citation(overrides: Partial<MessageCitation> & { chunk_id: string }): MessageCitation {
  return {
    document_id: null,
    filename: null,
    page: null,
    section: null,
    span: null,
    workspace_id: "ws-1",
    ...overrides,
  };
}

function payload(overrides: Partial<AnswerPayload> = {}): AnswerPayload {
  return {
    answer: "…",
    status: "answered",
    claims: [],
    citations: [],
    ...overrides,
  };
}

const FIRST = payload({
  answer: "Answer one.",
  status: "answered",
  confidence: { level: "high", basis: "both claims grounded", probability: 0.93 },
  limitations: ["only the Q1 report was searched"],
  claims: [{ text: "Revenue grew.", chunk_ids: ["chunk-a"] }],
  citations: [
    citation({
      chunk_id: "chunk-a",
      document_id: "doc-a",
      filename: "alpha.pdf",
      page: 3,
      content: "…revenue grew 12%…",
    }),
  ],
});

const SECOND = payload({
  answer: "Answer two.",
  status: "partial",
  confidence: { level: "low", basis: "one of two claims grounded", probability: 0.41 },
  limitations: [],
  claims: [{ text: "Headcount fell.", chunk_ids: ["chunk-b"] }],
  citations: [
    citation({
      chunk_id: "chunk-b",
      document_id: "doc-b",
      filename: "beta.pdf",
      page: 9,
      content: "…headcount fell…",
    }),
  ],
});

describe("answerMessage — citations live with their own message", () => {
  it("keeps two answers independent (different docs, both links correct)", () => {
    const messages = [
      userMessage("first question"),
      answerMessage(FIRST),
      userMessage("second question"),
      answerMessage(SECOND),
    ];

    expect(messages[1].citations.map((c) => c.filename)).toEqual(["alpha.pdf"]);
    expect(messages[3].citations.map((c) => c.filename)).toEqual(["beta.pdf"]);

    // Each message's claim still resolves to ITS OWN document.
    expect(citationForChunk(messages[1], "chunk-a")?.filename).toBe("alpha.pdf");
    expect(citationForChunk(messages[3], "chunk-b")?.filename).toBe("beta.pdf");

    // …and never to the other message's document.
    expect(citationForChunk(messages[1], "chunk-b")).toBeNull();
    expect(citationForChunk(messages[3], "chunk-a")).toBeNull();
  });

  it("does not mutate the payload the message was built from", () => {
    const frozen = Object.freeze({ ...FIRST, citations: Object.freeze([...FIRST.citations]) });
    const message = answerMessage(frozen as AnswerPayload);
    message.citations.pop();
    expect(FIRST.citations).toHaveLength(1);
  });

  it("stores confidence, limitations and status on that same message", () => {
    const one = answerMessage(FIRST);
    const two = answerMessage(SECOND);

    expect(one.status).toBe("answered");
    expect(one.confidence?.level).toBe("high");
    expect(one.limitations).toEqual(["only the Q1 report was searched"]);

    expect(two.status).toBe("partial");
    expect(two.confidence?.level).toBe("low");
    expect(two.limitations).toEqual([]);

    // Frozen at arrival: building the second message changed nothing about
    // the first — there is no shared/latest-answer slot to read from.
    expect(one.confidence?.probability).toBe(0.93);
    expect(one.citations[0].filename).toBe("alpha.pdf");
  });

  it("survives an absent/undefined optional field on the wire", () => {
    const message = answerMessage({
      answer: "…",
      status: "insufficient_evidence",
      claims: [],
      citations: [],
    } as AnswerPayload);
    expect(message.citations).toEqual([]);
    expect(message.limitations).toEqual([]);
    expect(message.reason).toBeNull();
  });
});

describe("failureMessage / userMessage — no borrowed evidence", () => {
  it("a failed turn carries zero citations", () => {
    const messages = [userMessage("q"), answerMessage(FIRST), userMessage("q2"), failureMessage(new Error("gateway down"))];
    const failed = messages[3];

    expect(failed.error).toBe(true);
    expect(failed.content).toContain("gateway down");
    expect(failed.citations).toEqual([]);
    expect(citationForChunk(failed, "chunk-a")).toBeNull();
  });

  it("string/non-Error failures still render a message with no citations", () => {
    const failed = failureMessage("plain failure");
    expect(failed.content).toContain("Something went wrong.");
    expect(failed.citations).toEqual([]);
  });

  it("a user turn owns an empty citation list", () => {
    const user = userMessage("what happened?");
    expect(user.role).toBe("user");
    expect(user.citations).toEqual([]);
    expect(citationForChunk(user, "chunk-a")).toBeNull();
  });
});

describe("citationForChunk — cross-message mixup guard", () => {
  it("returns null instead of searching other messages", () => {
    const messages = [answerMessage(FIRST), answerMessage(SECOND)];
    // The exact bug class the ticket exists for: resolving a claim's chunk
    // against "the latest answer" would yield beta.pdf here.
    expect(citationForChunk(messages[0], "chunk-b")).toBeNull();
    expect(citationForChunk(messages[1], "chunk-a")).toBeNull();
  });

  it("ignores duplicate chunk ids across messages independently", () => {
    const shared = citation({ chunk_id: "chunk-shared", filename: "same.pdf" });
    const messages = [
      answerMessage(payload({ citations: [shared] })),
      answerMessage(payload({ citations: [{ ...shared, filename: "other.pdf" }] })),
    ];
    expect(citationForChunk(messages[0], "chunk-shared")?.filename).toBe("same.pdf");
    expect(citationForChunk(messages[1], "chunk-shared")?.filename).toBe("other.pdf");
  });
});
