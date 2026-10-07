/**
 * Unit tests for the job/document status-chip mapping (TASK-M2-002a Tests:
 * "unit: status-chip mapping (all job states covered)").
 */
import { describe, expect, it } from "vitest";
import { jobStateTone, type ChipTone } from "./status-chip";
import type { JobState } from "./contracts";

const ALL_JOB_STATES: JobState[] = [
  "queued",
  "parsing",
  "chunking",
  "embedding",
  "ready",
  "failed",
];

describe("jobStateTone", () => {
  it("covers every known job state", () => {
    const expected: Record<JobState, ChipTone> = {
      queued: "pending",
      parsing: "running",
      chunking: "running",
      embedding: "running",
      ready: "success",
      failed: "danger",
    };
    for (const state of ALL_JOB_STATES) {
      expect(jobStateTone(state)).toBe(expected[state]);
    }
    // Every state is covered — no gaps in the mapping table.
    expect(new Set(ALL_JOB_STATES.map(jobStateTone))).toEqual(
      new Set(["pending", "running", "success", "danger"]),
    );
  });

  it("is case-insensitive", () => {
    expect(jobStateTone("READY")).toBe("success");
    expect(jobStateTone("Failed")).toBe("danger");
    expect(jobStateTone("QUEUED")).toBe("pending");
  });

  it("falls back to neutral for unknown/future states", () => {
    expect(jobStateTone("superseding_state")).toBe("neutral");
    expect(jobStateTone("")).toBe("neutral");
  });

  it("never throws for any string", () => {
    const samples = [...ALL_JOB_STATES, "partially_ready", "queued ", "RETRYING", "0"];
    for (const sample of samples) {
      expect(() => jobStateTone(sample)).not.toThrow();
    }
  });
});