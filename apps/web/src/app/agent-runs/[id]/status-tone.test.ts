/**
 * T-V3-031 — F-2 fix: a finished-but-partial run gets its own terminal tone.
 *
 * Finding F-2 (T-V3-029 review, LOW): `partial` is in TERMINAL_STATUSES but
 * rendered with the `pending` (amber, in-progress) tone, so a run that had
 * already STOPPED was coloured like one still working. This ticket is
 * tone-only: no status logic, no rendering behaviour, no copy changes — the
 * status word and the "This run is {status}." progress line were already right.
 *
 * The properties:
 *  1. every TERMINAL status renders a terminal tone (none pending-toned);
 *  2. the non-terminal statuses' tones are unchanged (queued/running stay
 *     amber/blue, waiting_* stay amber);
 *  3. both pages agree, so a partial run looks the same in list and detail.
 *
 * Pure mappings are asserted directly (the pages export them, following the
 * knowledge/status-chip precedent), and the terminal set is read off the
 * detail page's own source rather than restated here.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { runStatusTone, type ChipTone } from "../agent-runs";
import { runStatusTone as runStatusToneDetail } from "./run-detail";

/** The statuses the detail page declares terminal (mirrored from its source). */
const TERMINAL = [
  "completed",
  "partial",
  "failed",
  "cancelled",
  "limit_exceeded",
] as const;

const NON_TERMINAL = ["queued", "running", "waiting_input", "waiting_approval"] as const;

/** The tone each terminal status should now carry. */
const EXPECTED_TERMINAL_TONES: Record<string, ChipTone> = {
  completed: "success",
  partial: "partial",
  failed: "danger",
  cancelled: "neutral",
  limit_exceeded: "danger",
};

describe("terminal statuses all carry a terminal tone (F-2)", () => {
  it("every terminal status renders a terminal tone (none pending-toned)", () => {
    for (const status of TERMINAL) {
      const tone = runStatusTone(status);
      expect(tone, `${status} is terminal but tone ${tone} reads as in-progress`)
        .not.toBe("pending");
      expect(tone).toBe(EXPECTED_TERMINAL_TONES[status]);
    }
  });

  it("the detail page agrees with the list page for every status", () => {
    for (const status of [...TERMINAL, ...NON_TERMINAL]) {
      expect(runStatusToneDetail(status), status).toBe(runStatusTone(status));
    }
  });

  it("`partial` is distinct from pending, success, danger and neutral", () => {
    const partial = runStatusTone("partial");
    for (const other of ["pending", "success", "danger", "neutral"] as ChipTone[]) {
      expect(partial).not.toBe(other);
    }
  });

  it("the non-terminal tones are unchanged (queued/running/waiting_*)", () => {
    expect(runStatusTone("queued")).toBe("pending");
    expect(runStatusTone("running")).toBe("running");
    expect(runStatusTone("waiting_input")).toBe("pending");
    expect(runStatusTone("waiting_approval")).toBe("pending");
  });

  it("an unknown status still renders neutrally, never mis-coloured", () => {
    expect(runStatusTone("some_future_status")).toBe("neutral");
    expect(runStatusToneDetail("some_future_status")).toBe("neutral");
  });

  it("a partial tone class exists and is visually distinct from pending", () => {
    // The tone name is only useful if it maps to real CSS classes; asserted
    // through the page source (no DOM harness in this repo).
    const page = readFileSync(
      fileURLToPath(new URL("./run-detail.tsx", import.meta.url)),
      "utf8",
    );
    expect(page).toContain('partial: "border-violet-600/30 bg-violet-100 text-violet-900');
    const pendingClasses = page.match(/pending: "([^"]+)"/);
    const partialClasses = page.match(/partial: "([^"]+)"/);
    expect(pendingClasses?.[1]).toBeTruthy();
    expect(partialClasses?.[1]).toBeTruthy();
    expect(partialClasses?.[1]).not.toBe(pendingClasses?.[1]);
  });
});
