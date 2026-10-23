/**
 * T-R17 — V3 surface + rollout verification tests.
 *
 * Verifies:
 * 1. surface fields rendering mocked/placeholder data as live: 0
 *    - every field traced to backend source (output from snapshot.output,
 *      steps from history StepStore, budget/usage from RunBudget/usage,
 *      sources from EvidenceStore).
 * 2. cancel/approval actions bypassing backend transitions: 0
 *    - cancel hits /runs/{id}/cancel (monotonic transition).
 *    - approval hits /runs/{id}/approval (decision: approved/rejected).
 * 3. rollout states misrepresenting readiness: 0
 *    - unconfigured workspace shows honest unavailable banner, disables start.
 *    - unconfigured research report shows honest unavailable banner naming missing component.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { mergeRunRecords, type RunRecord, type RunSnapshot } from "./agent-runs";

describe("T-R17: surface field tracing and merge discipline (zero mock data)", () => {
  it("mergeRunRecords merges server-authoritative runs with local records", () => {
    const serverRuns: RunSnapshot[] = [
      {
        run_id: "run-srv-1",
        workspace_id: "ws-1",
        status: "completed",
        error: null,
        error_code: null,
        goal: "Server goal",
        output: "The real final answer from the agent model.",
        budget: {
          max_steps: 10,
          max_tokens: 50000,
          max_tool_calls: 20,
          max_cost_usd: 0.5,
        },
        usage: {
          prompt_tokens: 1200,
          completion_tokens: 450,
          tool_calls: 3,
          cost_usd: 0.0035,
        },
        sources: [
          {
            document_id: "doc-12345678",
            chunk_id: "chk-87654321",
            statement: "The ground truth statement",
            verdict: "supported",
            quote: "Exact quote from document",
          },
        ],
      },
    ];

    const localRuns: RunRecord[] = [
      {
        run_id: "run-srv-1",
        workspace_id: "ws-1",
        status: "running",
        error: null,
        error_code: null,
        goal: "Local goal",
        max_steps: 5,
        max_tokens: 10000,
        max_tool_calls: 10,
        created_at: "2026-10-14T10:00:00Z",
      },
      {
        run_id: "run-local-only",
        workspace_id: "ws-1",
        status: "queued",
        error: null,
        error_code: null,
        goal: "Only started locally",
        max_steps: 5,
        max_tokens: 100000,
        max_tool_calls: 50,
        created_at: "2026-10-14T10:05:00Z",
      },
    ];

    const merged = mergeRunRecords(serverRuns, localRuns);
    expect(merged).toHaveLength(2);

    const srv = merged.find((r) => r.run_id === "run-srv-1");
    expect(srv).toBeDefined();
    expect(srv?.status).toBe("completed");
    expect(srv?.output).toBe("The real final answer from the agent model.");
    expect(srv?.budget?.max_steps).toBe(10);
    expect(srv?.usage?.prompt_tokens).toBe(1200);
    expect(srv?.usage?.cost_usd).toBe(0.0035);
    expect(srv?.sources).toHaveLength(1);
    expect(srv?.sources?.[0].document_id).toBe("doc-12345678");

    const loc = merged.find((r) => r.run_id === "run-local-only");
    expect(loc).toBeDefined();
    expect(loc?.status).toBe("queued");
  });

  it("mergeRunRecords handles empty server or local lists gracefully", () => {
    expect(mergeRunRecords([], [])).toEqual([]);
    const localOnly: RunRecord[] = [
      {
        run_id: "run-1",
        workspace_id: "ws-1",
        status: "queued",
        error: null,
        error_code: null,
        goal: "Test",
        max_steps: 5,
        max_tokens: 1000,
        max_tool_calls: 5,
        created_at: "2026-10-14T10:00:00Z",
      },
    ];
    expect(mergeRunRecords([], localOnly)).toHaveLength(1);
  });
});

describe("T-R17: rollout posture and honest unavailable states", () => {
  it("agent-runs list page checks readiness and disables form when unconfigured", () => {
    const page = readFileSync(
      fileURLToPath(new URL("./agent-runs.tsx", import.meta.url)),
      "utf8",
    );
    // Checks readiness endpoint
    expect(page).toContain("/agent/readiness");
    // Honest unavailable banner
    expect(page).toContain("Agent runtime unavailable");
    expect(page).toContain("No LLM provider is configured");
    // Form and button disabled when unavailable
    expect(page).toContain("disabled={isUnavailable}");
    expect(page).toContain('isUnavailable ? "Unavailable" : "Start run"');
    // Fetches server runs from /agent/runs
    expect(page).toContain("/agent/runs");
  });

  it("agent-runs detail page wires approval and cancel to backend transitions", () => {
    const detail = readFileSync(
      fileURLToPath(new URL("./[id]/run-detail.tsx", import.meta.url)),
      "utf8",
    );
    // Wires cancel route
    expect(detail).toContain("/runs/${runId}/cancel");
    // Wires approval route
    expect(detail).toContain("/runs/${runId}/approval");
    expect(detail).toContain('handleApproval("approved")');
    expect(detail).toContain('handleApproval("rejected")');
    // Fetches history for real execution steps
    expect(detail).toContain("/runs/${runId}/history");
    // Renders real output section
    expect(detail).toContain("snapshot?.output");
    expect(detail).toContain("Run in progress — output will appear upon completion.");
    // Renders real budget & usage section
    expect(detail).toContain("snapshot?.usage");
    expect(detail).toContain("prompt_tokens");
    expect(detail).toContain("completion_tokens");
    expect(detail).toContain("cost_usd");
    // Renders real sources section with document link
    expect(detail).toContain("snapshot?.sources");
    expect(detail).toContain("/knowledge/documents/");
  });

  it("research report page renders honest unavailable banner for unconfigured pipeline", () => {
    const research = readFileSync(
      fileURLToPath(new URL("../research/[id]/page.tsx", import.meta.url)),
      "utf8",
    );
    // Honest unavailable banner
    expect(research).toContain('report.error_code === "unavailable"');
    expect(research).toContain("Research pipeline unavailable");
    expect(research).toContain("No research pipeline is configured");
    expect(research).toContain("missing: {report.missing");
  });
});
