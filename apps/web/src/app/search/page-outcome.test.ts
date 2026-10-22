/**
 * T-IMP-003 (round 2) — OUTCOME regression for the search workspace fence.
 *
 * The source-wiring assertions in `page-wiring.test.ts` cannot see a COMMIT
 * WINDOW: they only read the file. This test drives the REAL component in a
 * REAL browser (React 19 + real scheduling) and reproduces the interleaving
 * that the source assertions miss:
 *
 *   A search is in flight -> the workspace list is reloaded so B becomes
 *   active -> A's promise is released DURING B's layout commit -> A's result
 *   (or A's error) must never be painted under B.
 *
 * The release happens between B's render and B's passive effect, i.e. exactly
 * in the window where a passive-effect invalidation has not run yet. The
 * fence therefore has to be tied to the workspace IDENTITY of the published
 * value, not only to a run counter bumped in a passive effect.
 *
 * Legs (all must hold):
 *   1. commit-window SUCCESS  — A's result is not visible under B;
 *   2. commit-window ERROR    — A's error is not visible under B;
 *   3. control: ordinary late completion (after the passive effect) is
 *      discarded;
 *   4. control: a fresh B request still succeeds and is shown.
 *
 * The page under test can be pointed at another build with
 * `SEARCH_PAGE_PATH=<file>` — that is how the teeth are demonstrated (the
 * same test fails against the pre-fix page).
 *
 * The browser is skipped when none is installed (CI has no Chromium):
 * `CHROME_PATH` overrides the lookup.
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Plugin } from "esbuild";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const HERE = fileURLToPath(new URL(".", import.meta.url));
// apps/web (HERE is apps/web/src/app/search/)
const WEB_ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const PAGE_PATH = process.env.SEARCH_PAGE_PATH ?? join(HERE, "page.tsx");
const PROVIDER_PATH = join(WEB_ROOT, "src/components/workspace/workspace-provider.tsx");

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH ?? "",
  "/root/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell",
  "/root/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome",
  "/root/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell",
].filter(Boolean);

const chrome = CHROME_CANDIDATES.find((candidate) => existsSync(candidate));

const STUBS: Record<string, string> = {
  "./contracts": `
    export const listMyWorkspaces = async () =>
      window.workspaceList ?? [{ id: "A", name: "A", role: "owner" }];
    export const DEFAULT_SEARCH_MODE = "hybrid";
    export const EMPTY_SEARCH_FILTERS = {};
    export const buildSearchBody = (query, mode, filters) => ({ query, mode, filters });
    export const isValidDateFilter = () => true;
    export const searchDocuments = (workspace, body) =>
      new Promise((resolve, reject) => {
        window.trace.push("request:" + workspace);
        window.pending.push({ workspace, resolve, reject });
      });
  `,
  "./components/result-list": `
    export const ResultList = ({ results }) => (
      <div id="results">{results.map((r) => r.content).join("|")}</div>
    );
  `,
  "./components/result-detail": `
    export const ResultDetail = ({ item }) =>
      item ? <div id="detail">{item.content}</div> : null;
  `,
  "./components/filter-bar": `export const FilterBar = () => null;`,
  "./components/mode-toggle": `export const ModeToggle = () => null;`,
  "lucide-react": `export const Search = () => null;`,
};

const ENTRY = `
import React, { useEffect, useLayoutEffect } from "react";
import { createRoot } from "react-dom/client";
import Page from ${JSON.stringify(PAGE_PATH)};
import { WorkspaceProvider, useWorkspaces } from ${JSON.stringify(PROVIDER_PATH)};

window.trace = [];
window.pending = [];
window.reports = [];
const tick = () => new Promise((r) => setTimeout(r, 20));

function Observer() {
  const { active, reload } = useWorkspaces();
  const id = active?.id ?? null;
  window.switchTo = async () => {
    window.workspaceList = [{ id: "B", name: "B", role: "owner" }];
    await reload();
  };
  useLayoutEffect(() => {
    window.trace.push("layout:" + id);
    // Release A's in-flight request DURING B's layout commit: the window
    // between the render that switches the workspace and the passive effect
    // that would invalidate the run.
    if (id === "B" && window.releaseAt === "layout") {
      const pending = window.pending.shift();
      if (pending) {
        window.trace.push("released A at B layout");
        if (window.releaseMode === "error") pending.reject(new Error("A PRIVATE ERROR"));
        else pending.resolve({ results: [{ content: "A PRIVATE RESULT", workspace_id: "A" }] });
      }
    }
  }, [id]);
  useEffect(() => {
    window.trace.push("passive:" + id);
  }, [id]);
  return null;
}

function Harness() {
  return (
    <WorkspaceProvider>
      <Page />
      <Observer />
    </WorkspaceProvider>
  );
}

const host = document.createElement("div");
document.body.appendChild(host);
const report = document.createElement("pre");
report.id = "report";
document.body.appendChild(report);
const root = createRoot(host);

async function submit() {
  const input = document.querySelector("input");
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  setter.call(input, "salary");
  input.dispatchEvent(new Event("input", { bubbles: true }));
  await tick();
  document.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await tick();
}

(async () => {
  try {
    const legs = [
      { name: "commit-window success", releaseAt: "layout", releaseMode: "success" },
      { name: "commit-window error", releaseAt: "layout", releaseMode: "error" },
      { name: "late completion discarded", releaseAt: "passive", releaseMode: "success" },
    ];
    for (const leg of legs) {
      window.trace = [];
      window.pending = [];
      window.workspaceList = undefined;
      window.releaseAt = leg.releaseAt;
      window.releaseMode = leg.releaseMode;
      root.render(<Harness key={leg.name} />);
      await tick();
      await tick();
      await submit();
      if (window.pending.length !== 1) throw new Error(leg.name + ": no pending request");
      await window.switchTo();
      await tick();
      if (leg.releaseAt === "passive") {
        const pending = window.pending.shift();
        pending.resolve({ results: [{ content: "A PRIVATE RESULT", workspace_id: "A" }] });
        await tick();
      }
      window.reports.push({
        case: leg.name,
        privateVisible: host.textContent.includes("A PRIVATE RESULT"),
        privateErrorVisible: host.textContent.includes("A PRIVATE ERROR"),
        surface: host.textContent,
        trace: [...window.trace],
      });
    }

    // Control: a fresh B request must still succeed and be shown.
    window.trace = [];
    window.pending = [];
    window.workspaceList = undefined;
    window.releaseAt = "none";
    window.releaseMode = "success";
    root.render(<Harness key="fresh-b" />);
    await tick();
    await tick();
    await submit();
    if (window.pending.length !== 1) throw new Error("fresh-b: no pending request");
    await window.switchTo();
    await tick();
    await tick();
    await submit();
    const bRequest = window.pending.find((p) => p.workspace === "B");
    if (!bRequest || bRequest.workspace !== "B") throw new Error("fresh-b: no B request");
    bRequest.resolve({ results: [{ content: "B CURRENT RESULT", workspace_id: "B" }] });
    await tick();
    window.reports.push({
      case: "fresh B request succeeds",
      newBVisible: host.textContent.includes("B CURRENT RESULT"),
      privateVisible: host.textContent.includes("A PRIVATE RESULT"),
      surface: host.textContent,
    });

    report.textContent = JSON.stringify(window.reports);
  } catch (error) {
    report.textContent = JSON.stringify({ error: String(error), trace: window.trace });
  }
})();
`;

type Leg = {
  case: string;
  privateVisible?: boolean;
  privateErrorVisible?: boolean;
  newBVisible?: boolean;
  surface?: string;
  trace?: string[];
  error?: string;
};

const stubPlugin: Plugin = {
  name: "outcome-stubs",
  setup(build) {
    build.onResolve({ filter: /.*/ }, (args) =>
      Object.hasOwn(STUBS, args.path)
        ? { path: args.path, namespace: "outcome-stub" }
        : null,
    );
    build.onLoad({ filter: /.*/, namespace: "outcome-stub" }, (args) => ({
      contents: STUBS[args.path],
      loader: "tsx",
      resolveDir: WEB_ROOT,
    }));
  },
};

let browserReport: Leg[] | { error: string; trace?: string[] } | null = null;

function legs(): Leg[] {
  if (!Array.isArray(browserReport)) return [];
  return browserReport;
}

function legFor(name: string): Leg {
  const found = legs().find((r) => r.case === name);
  if (!found) throw new Error(`missing browser leg: ${name}`);
  return found;
}

describe.skipIf(!chrome)("T-IMP-003: the workspace fence holds in a real commit window", () => {
  let workdir = "";

  beforeAll(async () => {
    workdir = mkdtempSync(join(tmpdir(), "search-outcome-"));
    const esbuild = await import("esbuild");
    const outfile = join(workdir, "bundle.js");
    await esbuild.build({
      stdin: {
        contents: ENTRY,
        resolveDir: WEB_ROOT,
        loader: "tsx",
        sourcefile: "outcome-entry.tsx",
      },
      outfile,
      bundle: true,
      platform: "browser",
      format: "iife",
      jsx: "automatic",
      define: { "process.env.NODE_ENV": '"development"' },
      alias: { "@": join(WEB_ROOT, "src") },
      plugins: [stubPlugin],
    });
    writeFileSync(
      join(workdir, "index.html"),
      '<!doctype html><html><body><script src="bundle.js"></script></body></html>',
    );
    const dom = execFileSync(
      chrome as string,
      [
        "--headless",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        `--user-data-dir=${join(workdir, "profile")}`,
        "--virtual-time-budget=30000",
        "--dump-dom",
        `file://${join(workdir, "index.html")}`,
      ],
      { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 },
    );
    const match = dom.match(/<pre id="report">([\s\S]*?)<\/pre>/);
    if (!match) throw new Error(`no report in dumped DOM:\n${dom.slice(0, 2000)}`);
    const decoded = match[1]
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&amp;/g, "&");
    browserReport = JSON.parse(decoded);
  }, 120_000);

  afterAll(() => {
    if (workdir) rmSync(workdir, { recursive: true, force: true });
  });

  it("runs the browser scenario without an internal error", () => {
    expect((browserReport as { error?: string })?.error).toBeUndefined();
    expect(Array.isArray(browserReport)).toBe(true);
  });

  it("never paints A's result under B when A resolves in B's commit window", () => {
    const leg = legFor("commit-window success");
    expect(leg.privateVisible).toBe(false);
    expect(leg.surface).not.toContain("A PRIVATE RESULT");
  });

  it("never paints A's error under B when A rejects in B's commit window", () => {
    const leg = legFor("commit-window error");
    expect(leg.privateErrorVisible).toBe(false);
    expect(leg.surface).not.toContain("A PRIVATE ERROR");
  });

  it("discards an ordinary late completion from the previous workspace", () => {
    const leg = legFor("late completion discarded");
    expect(leg.privateVisible).toBe(false);
  });

  it("still lets a fresh request for the active workspace succeed", () => {
    const leg = legFor("fresh B request succeeds");
    expect(leg.newBVisible).toBe(true);
  });
});
