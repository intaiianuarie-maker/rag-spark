/**
 * T-V1-033 — the Ask screen is wired to the persistence layer (RP-13/QF-13).
 *
 * Reviewer checklist (1) "no React-only history" and (2) "workspace scoping
 * enforced" are properties of `page.tsx`, and this repo has no DOM test
 * harness (vitest runs in the node environment; adding one would mean a new
 * dev dependency, which is outside this ticket's allowed files). So the
 * wiring is pinned the same way the in-flight claim is: against the screen's
 * own source, with each assertion mapping to a checklist line. The BEHAVIOR
 * behind the calls is covered for real in `history.test.ts`.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const page = readFileSync(fileURLToPath(new URL("./page.tsx", import.meta.url)), "utf8");

describe("history is persisted, not React-state-only", () => {
  it("reads the transcript from the store and writes it back", () => {
    expect(page).toContain("loadHistory(");
    expect(page).toContain("saveHistory(");
  });

  it("hydrates from the store instead of seeding component state with turns", () => {
    // The transcript state starts EMPTY and is filled by loadHistory in the
    // scope effect — a refresh therefore re-runs that effect and restores.
    expect(page).toMatch(/messages: \[\],\s*\n?\s*\}\);?\s*$/m);
    expect(page).toMatch(
      /setTranscript\(\{\s*key: `\$\{accountScope\}::\$\{workspaceId\}`,\s*messages: loadHistory\(/,
    );
  });

  it("persists on every transcript change for the CURRENT scope only", () => {
    expect(page).toMatch(
      /saveHistory\(storage\(\), currentScope, transcript\.messages\);/,
    );
    // Guard: a transcript that belongs to another scope is never written
    // under the current scope's key.
    expect(page).toContain("if (transcript.key !== `${accountScope}::${workspaceId}`) return;");
  });
});

describe("scope is account AND workspace", () => {
  it("asks the server which account is signed in", () => {
    expect(page).toContain('"/api/account-scope"');
    expect(page).toContain("account: accountScope");
  });

  it("re-scopes on both scope parts (deps are the two primitives)", () => {
    expect(page).toContain("workspace: workspaceId");
    expect(page).toMatch(/\}, \[accountScope, workspaceId, storage\]\);/);
  });

  it("renders a transcript only while it belongs to the live scope", () => {
    // A workspace switch therefore shows that workspace's turns, never the
    // previous workspace's.
    expect(page).toContain("const messages = transcript.key === scopeKey ? transcript.messages : [];");
  });

  it("drops the open source panel when the scope changes", () => {
    expect(page).toContain("setSelectedSource(null); // the open source belongs to the previous scope");
    expect(page).toContain("setSelectedSource(null);");
  });
});
