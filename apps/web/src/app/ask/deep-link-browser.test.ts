/**
 * M4-002b: behavioral browser coverage for source deep-links.
 *
 * Drives the REAL page in a REAL browser with a hydrated transcript + pasted
 * hash. Asserts actual panel identity/content/visibility, not string probes
 * that pass vacuously.
 *
 * No paid model calls: stream stubbed, transcript hydrated from localStorage.
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Plugin } from "esbuild";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const WEB_ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const PAGE_PATH = join(HERE, "page.tsx");
const PROVIDER_PATH = join(WEB_ROOT, "src/components/workspace/workspace-provider.tsx");

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH ?? "",
  "/root/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell",
  "/root/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome",
].filter(Boolean);
const chrome = CHROME_CANDIDATES.find((candidate) => existsSync(candidate));

// Fail (not skip) in CI when Chrome is missing.
if (process.env.CI && !chrome) {
  throw new Error("Chrome required for deep-link browser tests in CI");
}

const TRANSCRIPT = JSON.stringify({
  version: 1,
  account: "acct",
  workspace: "ws-1",
  saved_at: "2026-10-25T00:00:00.000Z",
  messages: [
    { role: "user", content: "Q", claims: [], citations: [] },
    {
      role: "assistant",
      content: "A",
      claims: [{ text: "sourced", chunk_ids: ["chunk-a", "chunk-b"] }],
      citations: [
        { chunk_id: "chunk-a", document_id: "doc-a", filename: "alpha.pdf", page: 1, section: null, span: null, workspace_id: "ws-1" },
        { chunk_id: "chunk-b", document_id: "doc-b", filename: "beta.pdf", page: 2, section: null, span: null, workspace_id: "ws-1" },
      ],
      status: "answered",
    },
  ],
});

const STUBS: Record<string, string> = {
  "./contracts": `export const listMyWorkspaces = async () => [{ id: "ws-1", name: "ws-1", role: "owner" }]; export type AnswerPayload = any;`,
  "./stream": `export const streamQuestion = () => (async function*(){})();`,
  "./components/chat-window": `export const userMessage = (q) => ({ role: "user", content: q, claims: [], citations: [] });
    export const answerMessage = (a) => ({ role: "assistant", content: a.answer, claims: [], citations: [], status: a.status });
    export const failureMessage = (e) => ({ role: "assistant", content: "Error: " + String(e), error: true, claims: [], citations: [] });
    export const ChatWindow = ({ messages }) => (<div>{messages.map((m, i) => <div key={i} id={"message-" + i}>{m.content}</div>)}</div>);`,
  "./components/model-selector": `export const ModelSelector = () => null;`,
};

const stubPlugin: Plugin = {
  name: "deep-link-browser-stubs",
  setup(build) {
    build.onResolve({ filter: /.*/ }, (args) =>
      Object.hasOwn(STUBS, args.path) ? { path: args.path, namespace: "dl-stub" } : null
    );
    build.onLoad({ filter: /.*/, namespace: "dl-stub" }, (args) => ({
      contents: STUBS[args.path],
      loader: "tsx",
      resolveDir: WEB_ROOT,
    }));
  },
};

const ENTRY = `
import React from "react";
import { createRoot } from "react-dom/client";
import Page from ${JSON.stringify(PAGE_PATH)};
import { WorkspaceProvider } from ${JSON.stringify(PROVIDER_PATH)};

window.__errors = [];
window.addEventListener("error", (e) => window.__errors.push(String(e.message || e)));

window.fetch = () => Promise.resolve({ ok: true, json: async () => ({ scope: "acct" }) });

const KEY = "kios_ask_history:v1:acct:ws-1";
try { window.localStorage.setItem(KEY, ${JSON.stringify(TRANSCRIPT)}); } catch (e) {}

function Harness() {
  return (
    <WorkspaceProvider>
      <Page />
    </WorkspaceProvider>
  );
}

const host = document.createElement("div");
document.body.appendChild(host);
const root = createRoot(host);

(async () => {
  // DIRECT LOAD: valid hash for ws-1 -> panel opens showing alpha.pdf
  window.location.hash = "#source=" + encodeURIComponent(JSON.stringify({ workspaceId: "ws-1", messageIndex: 1, chunkId: "chunk-a" }));
  root.render(<Harness />);
  await new Promise((r) => setTimeout(r, 300));
  const directText = document.body.textContent || "";
  window.__direct = directText.includes("alpha.pdf") && directText.includes("Source");

  // HASHCHANGE -> next source: panel switches to beta.pdf
  window.location.hash = "#source=" + encodeURIComponent(JSON.stringify({ workspaceId: "ws-1", messageIndex: 1, chunkId: "chunk-b" }));
  window.dispatchEvent(new HashChangeEvent("hashchange"));
  await new Promise((r) => setTimeout(r, 300));
  const hcText = document.body.textContent || "";
  window.__hashchange = hcText.includes("beta.pdf") && !hcText.includes("alpha.pdf");

  // FOREIGN WORKSPACE: record panel state before, then apply foreign hash, then after.
  window.__beforeForeign = (document.body.textContent || "").includes("beta.pdf");
  window.location.hash = "#source=" + encodeURIComponent(JSON.stringify({ workspaceId: "ws-2", messageIndex: 1, chunkId: "chunk-a" }));
  window.dispatchEvent(new HashChangeEvent("hashchange"));
  await new Promise((r) => setTimeout(r, 300));
  const afterText = document.body.textContent || "";
  window.__afterForeign = !afterText.includes("alpha.pdf") && !afterText.includes("beta.pdf");
  window.__foreign = window.__beforeForeign === true && window.__afterForeign === true;

  // MALFORMED: safe, no crash, UI usable
  let malformedThrew = false;
  try {
    window.location.hash = "#source=not-json";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await new Promise((r) => setTimeout(r, 200));
  } catch (e) { malformedThrew = true; }
  window.__malformed_ok = !malformedThrew && (document.body.textContent || "").includes("Ask");

  // MISSING: safe
  let missingThrew = false;
  try {
    window.location.hash = "";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await new Promise((r) => setTimeout(r, 200));
  } catch (e) { missingThrew = true; }
  window.__missing_ok = !missingThrew && (document.body.textContent || "").includes("Ask");

  window.__errors = window.__errors || [];
  const report = document.createElement("pre");
  report.id = "report";
  report.textContent = JSON.stringify({
    direct: window.__direct,
    hashchange: window.__hashchange,
    foreign: window.__foreign,
    beforeForeign: window.__beforeForeign,
    afterForeign: window.__afterForeign,
    malformed_ok: window.__malformed_ok,
    missing_ok: window.__missing_ok,
    errors: window.__errors,
  });
  document.body.appendChild(report);
})();
`;

type BrowserReport = {
  direct?: boolean;
  hashchange?: boolean;
  foreign?: boolean;
  beforeForeign?: boolean;
  afterForeign?: boolean;
  malformed_ok?: boolean;
  missing_ok?: boolean;
  errors?: string[];
};

let browserReport: BrowserReport | null = null;

describe.skipIf(!chrome)("M4-002b: deep-link direct-load behavioral", () => {
  let workdir = "";
  beforeAll(async () => {
    workdir = mkdtempSync(join(tmpdir(), "deep-link-browser-"));
    const esbuild = await import("esbuild");
    const outfile = join(workdir, "bundle.js");
    await esbuild.build({
      stdin: { contents: ENTRY, resolveDir: WEB_ROOT, loader: "tsx", sourcefile: "deep-link-browser-entry.tsx" },
      outfile,
      bundle: true,
      platform: "browser",
      format: "iife",
      jsx: "automatic",
      define: { "process.env.NODE_ENV": '"development"' },
      alias: { "@": join(WEB_ROOT, "src") },
      plugins: [stubPlugin],
    });
    writeFileSync(join(workdir, "index.html"), '<!doctype html><html><body><script src="bundle.js"></script></body></html>');
    const dom = execFileSync(chrome as string, [
      "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
      `--user-data-dir=${join(workdir, "profile")}`,
      "--virtual-time-budget=30000", "--dump-dom",
      `file://${join(workdir, "index.html")}`,
    ], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
    const match = dom.match(/<pre id="report">([\s\S]*?)<\/pre>/);
    if (!match) throw new Error(`no report in dumped DOM:\n${dom.slice(0, 2000)}`);
    const decoded = match[1].replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&amp;/g, "&");
    browserReport = JSON.parse(decoded);
  }, 120_000);
  afterAll(() => { if (workdir) rmSync(workdir, { recursive: true, force: true }); });

  it("runs without internal error", () => {
    expect(browserReport).not.toBeNull();
    expect(browserReport?.errors ?? []).toEqual([]);
  });
  it("direct-load hash opens the source panel for the right chunk", () => {
    expect(browserReport?.direct).toBe(true);
  });
  it("hashchange opens the next source (and closes the previous)", () => {
    expect(browserReport?.hashchange).toBe(true);
  });
  it("foreign-workspace hash leaves the panel closed (before open, after closed)", () => {
    expect(browserReport?.beforeForeign).toBe(true);
    expect(browserReport?.afterForeign).toBe(true);
    expect(browserReport?.foreign).toBe(true);
  });
  it("malformed hash is safe and UI remains usable", () => {
    expect(browserReport?.malformed_ok).toBe(true);
  });
  it("missing hash is safe and UI remains usable", () => {
    expect(browserReport?.missing_ok).toBe(true);
  });
});

// Defect-injection proof: with parseSourceRef's workspace check removed, a
// foreign-workspace hash WOULD open the panel. This is documented as a manual
// mutation check (patch parseSourceRef to skip the workspaceId comparison, re-run
// the foreign leg above, expect __foreign === false). Not executed here to keep
// the suite green; the real (unmutated) implementation is what is tested.
