/** Real React page and auth serialization; only HTTP responses/workspace shell are fixtures. */
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Plugin } from "esbuild";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const WEB_ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const chrome = [process.env.CHROME_PATH ?? "",
  "/root/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell",
  "/root/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome",
].find((path) => path && existsSync(path));
if (process.env.CI && !chrome) throw new Error("Chrome required for eval browser checks");

const workspaceStub = `
import React, {createContext, useContext, useState} from "react";
const Context = createContext(null);
export function WorkspaceProvider({children}) {
  const [activeId, setActiveId] = useState("ws-1");
  window.setWorkspace = setActiveId;
  return <Context.Provider value={{activeId}}>{children}</Context.Provider>;
}
export function useWorkspaces() { return useContext(Context); }
`;
const stubPlugin: Plugin = {
  name: "workspace-shell",
  setup(build) {
    build.onResolve({ filter: /workspace-provider/ }, () => ({ path: "workspace", namespace: "fixture" }));
    build.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({ contents: workspaceStub, loader: "tsx", resolveDir: WEB_ROOT }));
  },
};

// Installed before the bundle imports the REAL auth client, so requests cross
// the production object -> JSON -> HTTP response boundary exactly once.
const fixtureServer = String.raw`
window.fixture = { calls: [], datasets: [], runs: [], deferred: null, pending: null };
const state = window.fixture;
const run = (id) => ({run_id:id, dataset_id:"ds-1", n_questions:1,
  results:{recall:0.75, precision:0.5, mrr:1}, cost:0, latency_ms:10, tokens:0,
  config_snapshot:{retrieval:{mode:"hybrid", top_k:10, reranker:true}}, accounting_complete:false});
state.datasets = [{id:"ds-1", name:"golden", workspace_id:"ws-1",
  examples:[{question:"q", expected_sources:["doc-1"], expected_answer:"", tags:["RO"]}]}];
state.runs = [run("run-baseline")];
window.fetch = async (url, init = {}) => {
  const path = String(url), method = init.method || "GET";
  const body = init.body ? JSON.parse(init.body) : null;
  if (body !== null && (typeof body !== "object" || Array.isArray(body))) throw Error("double serialized body");
  state.calls.push({path, method, body});
  const ws = path.includes("/ws-2/") ? "ws-2" : "ws-1";
  let kind;
  if (path.endsWith("/replay")) kind = "replay";
  else if (path.endsWith("/comparisons")) kind = "compare";
  else if (path.endsWith("/datasets")) kind = method === "POST" ? "create" : "datasets";
  else if (path.endsWith("/runs")) kind = method === "POST" ? "run" : "runs";
  else throw Error("unexpected fixture route " + path);
  const execute = () => {
    let payload;
    if (kind === "datasets") payload = ws === "ws-1" ? state.datasets : [];
    if (kind === "runs") payload = ws === "ws-1" ? state.runs : [];
    if (kind === "create") {
      payload = {id:"ds-new-" + state.calls.length, workspace_id:ws, name:body.name, examples:body.examples};
      if (ws === "ws-1") state.datasets.push(payload);
    }
    if (kind === "run" || kind === "replay") {
      payload = run(kind === "run" ? "run-new" : "run-replay");
      if (kind === "replay") payload.config_snapshot.replay_of = path.split("/").at(-2);
      if (ws === "ws-1") state.runs.push(payload);
    }
    if (kind === "compare") payload = {baseline_run_id:body.baseline_run_id,
      candidate_run_id:body.candidate_run_id, dimension:body.dimension, metric_deltas:[{metric:"recall",baseline:0.625,candidate:0.75,
        delta:0.125,direction:"improved",higher_is_better:true}],
      cost_delta:0,latency_delta_ms:0,tokens_delta:0,cost_direction:"unchanged",
      latency_direction:"unchanged",winners:{recall:"candidate"},configuration_validated:true};
    return new Response(JSON.stringify(payload), {status:200, headers:{"content-type":"application/json"}});
  };
  if (state.deferred === kind) {
    state.deferred = null;
    return new Promise((resolve) => {state.pending = {kind, resolve, execute};});
  }
  return execute();
};
`;

const ENTRY = String.raw`
import React from "react";
import {createRoot} from "react-dom/client";
import EvalPage from "@/app/eval/page";
import {WorkspaceProvider} from "@/components/workspace/workspace-provider";
const sleep = (ms=20) => new Promise(resolve => setTimeout(resolve, ms));
const text = () => document.body.textContent || "";
async function waitFor(predicate, label) {
  for (let i=0; i<150; i++) { if (predicate()) return; await sleep(); }
  throw Error("timed out: " + label);
}
const input = (placeholder) => document.querySelector('input[placeholder="' + placeholder + '"]');
async function fill(placeholder, value) {
  const node = input(placeholder);
  if (!node) throw Error("missing input " + placeholder);
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(node,value);
  node.dispatchEvent(new Event("input",{bubbles:true}));
  await sleep();
}
function click(label) {
  const button = Array.from(document.querySelectorAll("button")).find(node => node.textContent === label);
  if (!button) throw Error("missing button " + label);
  button.click();
}
function replayNew() {
  const row = Array.from(document.querySelectorAll('section[aria-label="Runs"] li')).find(node => node.querySelector("button")?.textContent === "run-new");
  if (!row) throw Error("run-new row missing");
  const button = Array.from(row.querySelectorAll("button")).find(node => node.textContent === "replay");
  if (!button) throw Error("run-new replay missing");
  button.click();
}
function detail() { return document.querySelector('[aria-label="Run detail"]'); }
const outcomes = {};
(async () => {
  const root = createRoot(document.getElementById("app"));
  root.render(<WorkspaceProvider><EvalPage/></WorkspaceProvider>);
  await waitFor(() => text().includes("run-baseline"), "initial lists");
  await sleep(100);
  outcomes.bounded = window.fixture.calls.length === 2;
  await fill("name", "golden-new");
  await fill("question", "Which source?");
  await fill("expected source ids (comma-separated)", "doc-1,doc-2");
  click("Create");
  await waitFor(() => text().includes("Run golden-new"), "created dataset readback");
  const created = window.fixture.calls.find(c => c.method === "POST" && c.path.endsWith("/datasets"));
  outcomes.created = created.body.examples[0].expected_sources.join(",") === "doc-1,doc-2";
  click("Run golden-new");
  await waitFor(() => detail()?.textContent.includes("run-new"), "run detail");
  await sleep(100);
  outcomes.run = detail()?.textContent.includes('"recall": 0.75') && detail()?.textContent.includes('"accounting_complete": false');
  outcomes.privacy = !detail().querySelector('input[type="checkbox"]').checked;
  // A create triggers an ordinary list refresh; selected result must survive it.
  await fill("name", "refresh-control"); await fill("question", "q"); click("Create");
  await waitFor(() => text().includes("Run refresh-control"), "same-scope refresh");
  outcomes.survives = detail()?.textContent.includes("run-new");
  replayNew();
  await waitFor(() => detail()?.textContent.includes("run-replay"), "replay detail");
  await sleep(100);
  outcomes.replay = detail()?.textContent.includes("replay_of: run-new");
  await fill("baseline run id", "run-baseline"); await fill("candidate run id", "run-new"); click("Compare");
  await waitFor(() => text().includes('"metric_deltas"') && text().includes('"delta": 0.125'), "comparison");
  outcomes.compare = true;

  outcomes.serialized = true;
  for (const kind of ["create", "run", "replay", "compare"]) {
    if (kind === "create") { await fill("name", "stale-create"); await fill("question", "q"); }
    if (kind === "compare") { await fill("baseline run id", "run-baseline"); await fill("candidate run id", "run-new"); }
    window.fixture.deferred = kind;
    if (kind === "replay") replayNew();
    else click(kind === "create" ? "Create" : kind === "run" ? "Run golden" : "Compare");
    await waitFor(() => window.fixture.pending?.kind === kind, "deferred " + kind);
    await sleep();
    const operationCalls = window.fixture.calls.length;
    const compareButton = Array.from(document.querySelectorAll("button")).find(node => node.textContent === "Compare");
    outcomes.serialized = outcomes.serialized && compareButton.disabled;
    compareButton.click();
    await sleep();
    outcomes.serialized = outcomes.serialized && window.fixture.calls.length === operationCalls;
    window.setWorkspace("ws-2");
    await waitFor(() => text().includes("Datasets (0)"), "workspace B");
    if (text().includes("run-new") || input("name").value) throw Error("workspace data leaked into B");
    window.setWorkspace("ws-1");
    await waitFor(() => text().includes("Run golden"), "new workspace A instance");
    // New A gets a live draft. Old A's completion must not clear it, publish
    // stale run/comparison state, or trigger extra list requests in the new tree.
    await fill("name", "new-A-draft");
    const before = window.fixture.calls.length;
    const pending = window.fixture.pending;
    window.fixture.pending = null;
    pending.resolve(pending.execute());
    await sleep(150);
    outcomes[kind + "Race"] = input("name").value === "new-A-draft" &&
      detail() === null && !text().includes('"delta"') && window.fixture.calls.length === before;
  }
})().catch(error => {outcomes.error = String(error?.stack || error);}).finally(() => {
  const report = document.createElement("pre"); report.id = "report";
  report.textContent = JSON.stringify(outcomes); document.body.appendChild(report);
});
`;

let report: Record<string, boolean | string> = {};
describe.skipIf(!chrome)("eval page browser outcomes", () => {
  let directory = "";
  beforeAll(async () => {
    directory = mkdtempSync(join(tmpdir(), "eval-browser-"));
    const esbuild = await import("esbuild");
    await esbuild.build({stdin:{contents:ENTRY, resolveDir:WEB_ROOT, loader:"tsx"},
      outfile:join(directory,"bundle.js"), bundle:true, platform:"browser", format:"iife",
      jsx:"automatic", define:{"process.env.NODE_ENV":'"development"'},
      alias:{"@":join(WEB_ROOT,"src")}, plugins:[stubPlugin]});
    writeFileSync(join(directory,"fixture.js"),fixtureServer);
    writeFileSync(join(directory,"index.html"), '<!doctype html><html><body><div id="app"></div><script src="fixture.js"></script><script src="bundle.js"></script></body></html>');
    const dom = execFileSync(chrome as string, ["--headless","--no-sandbox","--disable-gpu",
      "--disable-dev-shm-usage",`--user-data-dir=${join(directory,"profile")}`,
      "--virtual-time-budget=40000","--dump-dom",`file://${join(directory,"index.html")}`],
      {encoding:"utf8",timeout:90000,maxBuffer:8*1024*1024});
    const match = dom.match(/<pre id="report">([\s\S]*?)<\/pre>/);
    if (!match) throw Error(`browser produced no report: ${dom.slice(-1500)}`);
    report = JSON.parse(match[1].replace(/&quot;/g,'"').replace(/&lt;/g,"<").replace(/&gt;/g,">").replace(/&amp;/g,"&"));
    if (report.error) throw Error(String(report.error));
  },120000);
  afterAll(() => {if(directory) rmSync(directory,{recursive:true,force:true});});
  for (const outcome of ["bounded","created","run","privacy","survives","replay","compare",
    "serialized","createRace","runRace","replayRace","compareRace"]) {
    it(outcome, () => {expect(report[outcome]).toBe(true);});
  }
});
