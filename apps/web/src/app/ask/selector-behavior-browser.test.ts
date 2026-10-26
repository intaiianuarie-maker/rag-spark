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
if (process.env.CI && !chrome) throw new Error("Chrome required for selector browser checks");

const workspaceStub = `
import React, {createContext, useContext, useState} from "react";
const Context = createContext(null);
export function WorkspaceProvider({children}) {
  const [activeId, setActiveId] = useState("ws-1");
  window.setWorkspace = setActiveId;
  return <Context.Provider value={{activeId, active:activeId ? {id:activeId,name:activeId,role:"owner"} : null}}>{children}</Context.Provider>;
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
window.fixture = {calls:[], streams:[], pending:[], deferred:[], failure:false};
const state = window.fixture;
const json = (data, status=200) => new Response(JSON.stringify(data),
  {status,headers:{"content-type":"application/json"}});
window.fetch = async (url, init={}) => {
  const path = String(url);
  state.calls.push(path);
  if(path === "/api/account-scope") return json({scope:"fixture-account"});
  const ws = path.includes("/ws-2/") ? "ws-2" : "ws-1";
  if(path.endsWith("/model-configs")) {
    if(state.failure) return json({message:"config unavailable"},503);
    const payload = [{preferred_model:ws === "ws-1" ? "model-a" : "model-b",
      extra_models:[{id:"extra-1"}]}];
    if(state.deferred.includes(ws)) {
      state.deferred.splice(state.deferred.indexOf(ws),1);
      return new Promise(resolve => state.pending.push({ws,resolve,payload}));
    }
    return json(payload);
  }
  if(path.endsWith("/collections")) return json([{id:ws,name:"Collection " + ws}]);
  if(path.endsWith("/ask/stream")) {
    const body = JSON.parse(init.body);
    state.streams.push({ws,body});
    const answer = {answer:"Fixture answer",status:"answered",claims:[],citations:[]};
    return new Response('event: token\ndata: {"text":"Fixture answer"}\n\n' +
      'event: citations\ndata: '+JSON.stringify(answer)+'\n\n',
      {status:200,headers:{"content-type":"text/event-stream"}});
  }
  throw Error("Unexpected route " + path);
};
window.resolveMetadata = (index, stale=false) => {
  const item = state.pending.splice(index,1)[0];
  if(!item) throw Error("No pending metadata");
  item.resolve(json(stale ? [{preferred_model:"stale-marker"}] : item.payload));
};
`;

const ENTRY = String.raw`
import React from "react";
import {createRoot} from "react-dom/client";
import AskPage from "@/app/ask/page";
import {WorkspaceProvider} from "@/components/workspace/workspace-provider";
const sleep = (ms=20) => new Promise(resolve=>setTimeout(resolve,ms));
async function waitFor(predicate,label) {
  for(let i=0;i<150;i++) {if(predicate()) return; await sleep();}
  throw Error("timed out: "+label);
}
const button = () => document.querySelector('button[aria-label="Select model"]');
const options = () => Array.from(document.querySelectorAll('[role="option"]'));
const outcomes = {};
async function openModels() {
  if(!document.querySelector('[role="listbox"]')) button().click();
  await waitFor(()=>document.querySelector('[role="listbox"]'),"model menu");
}
async function send() {
  const input = document.querySelector('input[placeholder="Ask anything…"]');
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(input,"A test question");
  input.dispatchEvent(new Event("input",{bubbles:true}));
  await sleep();
  const previous = window.fixture.streams.length;
  Array.from(document.querySelectorAll("button")).find(node=>node.textContent==="Send").click();
  await waitFor(()=>window.fixture.streams.length>previous,"actual stream request");
  await waitFor(()=>!document.querySelector('input[placeholder="Ask anything…"]').disabled,"stream finalization");
}
(async()=>{
  createRoot(document.getElementById("app")).render(<WorkspaceProvider><AskPage/></WorkspaceProvider>);
  await waitFor(()=>window.fixture.calls.some(path=>path.endsWith("/model-configs")),"metadata request");
  await openModels();
  await waitFor(()=>options().some(node=>node.textContent==="model-a"),"configured models");
  outcomes.configured = options().some(node=>node.textContent==="extra-1");
  outcomes.collection = document.querySelector('select[aria-label="Collection"]').value === "ws-1" &&
    document.querySelector('select[aria-label="Collection"]').textContent.includes("Collection ws-1");
  await waitFor(()=>document.activeElement?.getAttribute("role")==="option","initial keyboard focus");
  document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"ArrowDown",bubbles:true}));
  await waitFor(()=>document.activeElement?.textContent==="model-a","arrow focus");
  outcomes.keyboard = true;
  document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true}));
  await waitFor(()=>button().textContent.includes("model-a"),"selected model");
  await send();
  outcomes.override = window.fixture.streams.at(-1).body.model === "model-a";

  // Pause both new metadata requests: old A's selection must not be reused
  // either in B or when returning to A before either request resolves.
  await openModels();
  await waitFor(()=>document.activeElement?.textContent==="model-a","selected option focus");
  window.fixture.deferred.push("ws-2","ws-1");
  window.setWorkspace("ws-2");
  await waitFor(()=>window.fixture.pending.length===1,"delayed B metadata");
  outcomes.openMenuReset = document.querySelector('[role="listbox"]') === null;
  outcomes.scopeReset = button().textContent.includes("Workspace default") &&
    !document.body.textContent.includes("Collection ws-1");
  await send();
  outcomes.defaultBody = window.fixture.streams.at(-1).ws === "ws-2" &&
    !Object.hasOwn(window.fixture.streams.at(-1).body,"model");
  window.setWorkspace("ws-1");
  await waitFor(()=>window.fixture.pending.length===2,"new A metadata");
  outcomes.roundtripReset = button().textContent.includes("Workspace default");
  window.resolveMetadata(0,true); // B finishes after new A is already mounted.
  await sleep(100); await openModels();
  outcomes.stale = !options().some(node=>node.textContent==="stale-marker");
  document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}));
  window.resolveMetadata(0);
  await openModels();
  await waitFor(()=>options().some(node=>node.textContent==="model-a"),"fresh A options");
  button().click();
  window.fixture.failure = true;
  window.setWorkspace("ws-2");
  await waitFor(()=>document.body.textContent.includes("Configured models unavailable"),"failure disclosure");
  outcomes.fallback = button().textContent.includes("Workspace default");
  await send();
  outcomes.fallbackBody = !Object.hasOwn(window.fixture.streams.at(-1).body,"model");
})().catch(error=>{outcomes.error=String(error?.stack||error);}).finally(()=>{
  const report=document.createElement("pre");report.id="report";
  report.textContent=JSON.stringify(outcomes);document.body.appendChild(report);
});
`;

let report: Record<string, boolean | string> = {};
describe.skipIf(!chrome)("selector page browser outcomes", () => {
  let directory = "";
  beforeAll(async () => {
    directory = mkdtempSync(join(tmpdir(), "selector-browser-"));
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
  for (const outcome of ["configured","collection","keyboard","override","scopeReset",
    "openMenuReset","defaultBody","roundtripReset","stale","fallback","fallbackBody"]) {
    it(outcome, () => {expect(report[outcome]).toBe(true);});
  }
});
