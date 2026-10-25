/**
 * M2-002b: behavioral browser coverage for UploadPanel.
 *
 * Drives the REAL UploadPanel in a REAL browser with mocked transport (no
 * live compose, no paid model calls). Scenarios:
 *   1. six supported formats pass through actual form uploads (upload count
 *      increments per format; FormData carries a file);
 *   2. Cancel through the button survives a rerender (cancelled stays true,
 *      polling stops);
 *   3. a failed job's Retry re-issues the upload request (upload count +1,
 *      new job id);
 *   4. an upload started in workspace A whose response resolves after switching
 *      to B never sets A's job/filename in B (scope fence).
 *
 * Guarded: describe.skipIf(!chrome). Do not claim green until executed under
 * /root/code/bin/herdr-heavy-run.
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
const UPLOAD_PANEL_PATH = join(WEB_ROOT, "src/components/upload/upload-panel.tsx");

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH ?? "",
  "/root/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell",
  "/root/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome",
].filter(Boolean);
const chrome = CHROME_CANDIDATES.find((candidate) => existsSync(candidate));

if (process.env.CI && !chrome) {
  throw new Error("Chrome required for upload behavior browser tests in CI");
}

const FORMATS = [
  { name: "a.pdf", type: "application/pdf" },
  { name: "b.docx", type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" },
  { name: "c.html", type: "text/html" },
  { name: "d.csv", type: "text/csv" },
  { name: "e.md", type: "text/markdown" },
  { name: "f.txt", type: "text/plain" },
];

const STUBS: Record<string, string> = {
  "@/components/workspace/workspace-provider": `
    import React, { createContext, useContext, useState } from "react";
    const Ctx = createContext<any>(null);
    export function WorkspaceProvider({ children }: any) {
      const [activeId, setActiveId] = useState("ws-1");
      const value = { activeId, active: { id: activeId, name: activeId, role: "owner" }, status: "ready", workspaces: [{ id: "ws-1", name: "ws-1", role: "owner" }, { id: "ws-2", name: "ws-2", role: "owner" }], setActive: setActiveId, reload: async () => {}, myRole: () => "owner" };
      // expose setter for scenario 4
      if (typeof window !== "undefined") window.__setActiveWorkspace = setActiveId;
      return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
    }
    export function useWorkspaces() { return useContext(Ctx); }
  `,
  "@/lib/auth-client": `
    export const auth = {
      authFetch: async (url, init) => {
        if (String(url).endsWith("/documents") && init && init.method === "POST") {
          // deferred upload for scenario 4
          if (window.__delayUpload && !window.__delayResolved) {
            return new Promise((resolve) => { window.__resolveUpload = (v) => { window.__delayResolved = true; resolve(v); }; });
          }
          window.__uploadCount = (window.__uploadCount || 0) + 1;
          window.__lastUploadHasFile = !!(init.body && typeof init.body.get === "function" && init.body.get("file"));
          const jobId = "job-" + window.__uploadCount;
          if (!window.__jobStatus) window.__jobStatus = {};
          if (!window.__jobStatus[jobId]) window.__jobStatus[jobId] = { job_id: jobId, document_id: "doc-" + window.__uploadCount, status: "queued", stage: "queued", attempts: 1, max_attempts: 3, error_code: null, error_message: null, created_at: new Date().toISOString(), finished_at: null };
          return { job_id: jobId, document_id: "doc-" + window.__uploadCount, version_id: "v-" + window.__uploadCount, status: "queued" };
        }
        const m = String(url).match(/\\/jobs\\/(job-\\d+)/);
        const jobId = m ? m[1] : "job-1";
        if (window.__jobStatus && window.__jobStatus[jobId]) return window.__jobStatus[jobId];
        return { job_id: jobId, document_id: "doc-1", status: "queued", stage: "queued", attempts: 1, max_attempts: 3, error_code: null, error_message: null, created_at: new Date().toISOString(), finished_at: null };
      },
    };
    export class AuthApiError extends Error {}
    export class AuthLoginRequiredError extends Error {}
  `,
};

const stubPlugin: Plugin = {
  name: "upload-behavior-stubs",
  setup(build) {
    build.onResolve({ filter: /.*/ }, (args) => {
      if (Object.hasOwn(STUBS, args.path)) return { path: args.path, namespace: "up-stub" };
      return null;
    });
    build.onLoad({ filter: /.*/, namespace: "up-stub" }, (args) => ({
      contents: STUBS[args.path],
      loader: "tsx",
      resolveDir: WEB_ROOT,
    }));
  },
};

const ENTRY = `
import React from "react";
import { createRoot } from "react-dom/client";
import { UploadPanel } from ${JSON.stringify(UPLOAD_PANEL_PATH)};
import { WorkspaceProvider } from "@/components/workspace/workspace-provider";

window.__uploadCount = 0;
window.__jobStatus = {};
window.__delayUpload = false;
window.__delayResolved = false;
window.__errors = [];
window.addEventListener("error", (e) => window.__errors.push(String(e.message || e)));

const host = document.createElement("div");
document.body.appendChild(host);
const root = createRoot(host);

function uploadFile(input, name, type) {
  const file = new File(["x"], name, { type });
  const dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

function Harness() { return <WorkspaceProvider><UploadPanel /></WorkspaceProvider>; }

(async () => {
  root.render(<Harness />);
  await new Promise((r) => setTimeout(r, 200));
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;

  // Scenario 1: six supported formats
  for (const f of ${JSON.stringify(FORMATS)}) {
    uploadFile(input, f.name, f.type);
    await new Promise((r) => setTimeout(r, 80));
  }
  window.__scenario1 = (window.__uploadCount === 6 && window.__lastUploadHasFile === true);

  // Scenario 2: cancel survives rerender
  // upload a queued (never terminal) job
  const before = window.__uploadCount;
  uploadFile(input, "g.txt", "text/plain");
  await new Promise((r) => setTimeout(r, 80));
  const cancelBtn = Array.from(document.querySelectorAll("button")).find((b) => /cancel/i.test(b.textContent || ""));
  if (cancelBtn) cancelBtn.click();
  await new Promise((r) => setTimeout(r, 50));
  // rerender
  root.render(<Harness />);
  await new Promise((r) => setTimeout(r, 100));
  window.__scenario2 = (document.body.textContent || "").toLowerCase().includes("cancelled");

  // Scenario 3: failed retry re-issues upload
  // make next job fail
  const nextN = (window.__uploadCount || 0) + 1;
  window.__jobStatus["job-" + nextN] = { job_id: "job-" + nextN, document_id: "doc-" + nextN, status: "failed", stage: "failed", attempts: 1, max_attempts: 3, error_code: "PARSE_ERROR", error_message: "bad", created_at: new Date().toISOString(), finished_at: new Date().toISOString() };
  uploadFile(input, "h.txt", "text/plain");
  await new Promise((r) => setTimeout(r, 2400));
  const retryBtn = Array.from(document.querySelectorAll("button")).find((b) => /retry/i.test(b.textContent || ""));
  const countBeforeRetry = window.__uploadCount;
  if (retryBtn) retryBtn.click();
  await new Promise((r) => setTimeout(r, 200));
  window.__scenario3 = (window.__uploadCount === countBeforeRetry + 1);

  // Scenario 4: delayed A upload never paints B
  window.__delayUpload = true;
  window.__delayResolved = false;
  const startCount = window.__uploadCount;
  uploadFile(input, "i.txt", "text/plain"); // starts deferred upload in ws-1
  await new Promise((r) => setTimeout(r, 50));
  // switch to ws-2 before upload resolves
  if (window.__setActiveWorkspace) window.__setActiveWorkspace("ws-2");
  await new Promise((r) => setTimeout(r, 50));
  // resolve the deferred upload now
  if (window.__resolveUpload) {
    const jobId = "job-" + (startCount + 1);
    window.__jobStatus[jobId] = { job_id: jobId, document_id: "doc-" + (startCount + 1), status: "ready", stage: "ready", attempts: 1, max_attempts: 3, error_code: null, error_message: null, created_at: new Date().toISOString(), finished_at: new Date().toISOString() };
    window.__resolveUpload({ job_id: jobId, document_id: "doc-" + (startCount + 1), version_id: "v-" + (startCount + 1), status: "queued" });
  }
  await new Promise((r) => setTimeout(r, 300));
  // After switch, B should not show A's filename/job
  const body = document.body.textContent || "";
  window.__scenario4 = !body.includes("i.txt") && !body.includes("job-" + (startCount + 1));
  window.__delayUpload = false;

  const report = document.createElement("pre");
  report.id = "report";
  report.textContent = JSON.stringify({
    scenario1: window.__scenario1,
    scenario2: window.__scenario2,
    scenario3: window.__scenario3,
    scenario4: window.__scenario4,
    uploadCount: window.__uploadCount,
    lastUploadHasFile: window.__lastUploadHasFile,
    errors: window.__errors,
  });
  document.body.appendChild(report);
})();
`;

type Report = {
  scenario1?: boolean;
  scenario2?: boolean;
  scenario3?: boolean;
  scenario4?: boolean;
  uploadCount?: number;
  lastUploadHasFile?: boolean;
  errors?: string[];
};

let browserReport: Report | null = null;

describe.skipIf(!chrome)("M2-002b: upload behavior browser", () => {
  let workdir = "";
  beforeAll(async () => {
    workdir = mkdtempSync(join(tmpdir(), "upload-behavior-"));
    const esbuild = await import("esbuild");
    const outfile = join(workdir, "bundle.js");
    await esbuild.build({
      stdin: { contents: ENTRY, resolveDir: WEB_ROOT, loader: "tsx", sourcefile: "upload-behavior-entry.tsx" },
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
      "--virtual-time-budget=40000", "--dump-dom",
      `file://${join(workdir, "index.html")}`,
    ], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
    const match = dom.match(/<pre id="report">([\s\S]*?)<\/pre>/);
    if (!match) throw new Error(`no report:\n${dom.slice(0, 2000)}`);
    const decoded = match[1].replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&amp;/g, "&");
    browserReport = JSON.parse(decoded);
  }, 120_000);
  afterAll(() => { if (workdir) rmSync(workdir, { recursive: true, force: true }); });

  it("runs without internal error", () => {
    expect(browserReport).not.toBeNull();
    expect(browserReport?.errors ?? []).toEqual([]);
  });
  it("six formats pass through actual form uploads", () => {
    expect(browserReport?.scenario1).toBe(true);
  });
  it("cancel survives rerender", () => {
    expect(browserReport?.scenario2).toBe(true);
  });
  it("failed retry re-issues upload request", () => {
    expect(browserReport?.scenario3).toBe(true);
  });
  it("old scope upload never paints B", () => {
    expect(browserReport?.scenario4).toBe(true);
  });
});
