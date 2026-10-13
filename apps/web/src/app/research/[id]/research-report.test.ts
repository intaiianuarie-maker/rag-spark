/**
 * T-V3-024 smoke: research report render contract — unknowns + contradictions.
 *
 * Metric: unknowns hidden: 0.
 *
 * Repo pattern note: this package has no DOM testing library (see
 * `ask/citations.test.ts`, which tests pure message builders rather than the
 * React tree). This test follows that pattern: it exercises the page's own
 * pure render-contract helpers — `buildReportSections`, `isDisputed`, and
 * `chunkDeepLink` — which are the exact decisions the reviewer checklist
 * covers: exact clicks, disputed distinct, unknowns shown.
 */

import { describe, expect, it } from "vitest";

import {
  buildReportSections,
  chunkDeepLink,
  isDisputed,
} from "./report-sections";

const REPORT = {
  research_id: "r-1",
  workspace_id: "ws-test-1",
  status: "completed",
  topic: "test topic",
  grounded_sentences: [
    {
      statement: "Supported statement one.",
      chunk_ids: ["aaaaaaaa-1111-2222-3333-444444444444"],
      document_id: "dddddddd-1111-2222-3333-444444444444",
      chunk_id: "aaaaaaaa-1111-2222-3333-444444444444",
      confidence: "HIGH",
    },
    {
      statement: "Disputed-looking statement.",
      chunk_ids: ["bbbbbbbb-1111-2222-3333-444444444444"],
      document_id: "dddddddd-1111-2222-3333-444444444444",
      chunk_id: "bbbbbbbb-1111-2222-3333-444444444444",
      verdict: "DISPUTED",
    },
  ],
  unknowns: [
    { question: "What about X?", reason: "NO_EVIDENCE", statement: "No verified evidence for X." },
    { question: "What about Y?", reason: "NOT_GROUNDED", statement: "Y could not be grounded." },
  ],
  contradictions: [
    {
      question: "Is Z true?",
      claim_a: "Z is true.",
      claim_b: "Z is false.",
      chunk_ids_a: ["cca1a1a1-1111-2222-3333-444444444444"],
      chunk_ids_b: ["ccb2b2b2-1111-2222-3333-444444444444"],
      evidence_a_id: "ea",
      evidence_b_id: "eb",
      reason: "CONTRADICTORY_CLAIMS",
    },
  ],
  contradiction_outline: ["Is Z true?"],
  citations: [],
  abstained: false,
  abstention_reason: "",
  searched: ["s1"],
  limitations: [],
};

describe("research report render contract (T-V3-024)", () => {
  it("unknowns section is always built (unknowns hidden: 0)", () => {
    const sections = buildReportSections(REPORT);
    expect(sections.unknowns).toHaveLength(2);
    expect(sections.unknowns[0]).toEqual({
      reason: "NO_EVIDENCE",
      text: "No verified evidence for X.",
    });
    // Unknowns render regardless of whether contradictions exist.
    const bare = buildReportSections({ ...REPORT, contradictions: [] });
    expect(bare.unknowns).toHaveLength(2);
  });

  it("contradictions render as two sides, never smoothed", () => {
    const { contradictions } = buildReportSections(REPORT);
    expect(contradictions).toHaveLength(1);
    const c = contradictions[0];
    expect(c.sideA.claim).toBe("Z is true.");
    expect(c.sideB.claim).toBe("Z is false.");
    expect(c.sideA.claim).not.toBe(c.sideB.claim); // distinct sides
    expect(c.sideA.chunkLinks).toEqual(["/knowledge/documents/unknown/cca1a1a1-1111-2222-3333-444444444444"]);
    expect(c.sideB.chunkLinks).toEqual(["/knowledge/documents/unknown/ccb2b2b2-1111-2222-3333-444444444444"]);
  });

  it("evidence rows carry exact chunk deep links", () => {
    const { evidence } = buildReportSections(REPORT);
    expect(evidence).toHaveLength(2);
    expect(evidence[0].chunkLinks).toEqual([
      "/knowledge/documents/dddddddd-1111-2222-3333-444444444444/aaaaaaaa-1111-2222-3333-444444444444",
    ]);
    expect(evidence[0].statement).toBe("Supported statement one.");
  });

  it("marks disputed evidence distinctly", () => {
    const { evidence } = buildReportSections(REPORT);
    expect(evidence[0].disputed).toBe(false);
    expect(evidence[1].disputed).toBe(true);
    expect(evidence[1].statement).toBe("Disputed-looking statement.");
  });

  it("marks contradicted claims distinct from supported ones", () => {
    expect(isDisputed({ verdict: "DISPUTED" })).toBe(true);
    expect(isDisputed({ verdict: "SUPPORTED" })).toBe(false);
    expect(isDisputed({ disputed: true })).toBe(true);
    expect(isDisputed({})).toBe(false);
  });

  it("chunkDeepLink points at exact doc/version/chunk", () => {
    expect(chunkDeepLink("c1", "d1")).toBe("/knowledge/documents/d1/c1");
    expect(chunkDeepLink("c1", null)).toBe("/knowledge/documents/unknown/c1");
    expect(chunkDeepLink("", "d1")).toBeNull();
  });

  it("abstention is surfaced, not hidden", () => {
    const s = buildReportSections({ ...REPORT, abstained: true, abstention_reason: "no evidence" });
    expect(s.abstained).toBe(true);
    expect(s.abstentionReason).toBe("no evidence");
  });
});
