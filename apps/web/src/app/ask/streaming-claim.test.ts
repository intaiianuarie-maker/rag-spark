/**
 * T-V1-033 — honest answer-in-flight claim (RP-13/QF-13).
 *
 * The Ask screen answers in ONE request → ONE response; nothing about the
 * answer arrives incrementally. The ticket allows either implementing that
 * transport or removing the claim, and this ticket removes the claim: the
 * flag is named `pending`, the copy says an answer is pending, and the audit
 * below fails if any word implying incremental delivery comes back.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { PENDING_LABEL } from "./components/chat-window";

function readSource(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");
}

const SCREENS = ["./page.tsx", "./components/chat-window.tsx"] as const;

describe("the Ask screen makes no claim it does not honor", () => {
  it.each(SCREENS)("%s contains no incremental-delivery wording at all", (file) => {
    // Comments included on purpose: a claim in a comment is still a claim
    // a future reader would act on.
    expect(readSource(file)).not.toMatch(/stream/i);
  });

  it("the in-flight copy only says that an answer is pending", () => {
    expect(PENDING_LABEL).toBe("Waiting for the answer…");
    expect(PENDING_LABEL).not.toMatch(/stream|token|typing|live/i);
  });

  it("the flag is `pending` on both sides of the boundary", () => {
    expect(readSource("./components/chat-window.tsx")).toContain("pending: boolean");
    expect(readSource("./page.tsx")).toContain("pending={pending}");
    expect(readSource("./page.tsx")).not.toMatch(/streaming/i);
  });

  it("the pending indicator is announced and disables the composer", () => {
    const window = readSource("./components/chat-window.tsx");
    expect(window).toContain('aria-live="polite"');
    expect(window).toContain("{PENDING_LABEL}");
    expect(window).toContain("disabled={pending}");
  });
});
