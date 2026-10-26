/**
 * TASK-M5-001c — Structural selector wiring; browser outcomes live separately.
 *
 * The selector must list configured models, flow the override into the
 * request, and expose accessible controls. vitest runs in node, so the wiring
 * is pinned against the screen's own source; a11y is checked structurally.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const page = readFileSync(fileURLToPath(new URL("./page.tsx", import.meta.url)), "utf8");
const selector = readFileSync(fileURLToPath(new URL("./components/model-selector.tsx", import.meta.url)), "utf8");

describe("model selector wiring", () => {
  it("renders ModelSelector with fetched workspace model options", () => {
    expect(page).toContain("ModelSelector");
    expect(page).toContain("options={modelOptions}");
    expect(page).toContain("/v1/workspaces/${ws}/model-configs");
  });

  it("tracks selectedModel state and passes it to streamQuestion", () => {
    expect(page).toContain("selectedModel");
    expect(page).toContain("streamQuestion(workspaceAtSend, query, selectedModel");
  });

  it("selector exposes listbox + option roles and aria-expanded", () => {
    expect(selector).toContain('role="listbox"');
    expect(selector).toContain('role="option"');
    expect(selector).toContain("aria-expanded={open}");
    expect(selector).toContain('aria-haspopup="listbox"');
  });

  it("selector announces the selected value (aria-live) and is keyboard operable", () => {
    expect(selector).toContain('aria-live="polite"');
    expect(selector).toContain("ArrowDown");
    expect(selector).toContain("ArrowUp");
    expect(selector).toContain("Escape");
    expect(selector).toContain("aria-label=\"Select model\"");
  });

  it("selector closes on outside click and on Escape", () => {
    expect(selector).toContain("mousedown");
    expect(selector).toContain('addEventListener("keydown"');
  });
});
