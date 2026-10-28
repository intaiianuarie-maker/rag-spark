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
if (process.env.CI && !chrome) throw new Error("Chrome required for Sources browser checks");

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
window.fixture = {calls:[],sources:[],jobs:{},pending:null,deferTrigger:false,counter:0,deferInitial:true,releaseInitial:null};
const state=window.fixture;
const json=(payload,status=200)=>new Response(JSON.stringify(payload),{status,headers:{"content-type":"application/json"}});
const initial = "2026-01-01T00:00:00.000Z";
const statusPayload=(id,job,stage)=>({connector_id:id,kind:"s3",
  last_run_id:stage==="history" ? job.previousRun : job.run,
  last_finished_at:stage==="success" || stage==="failure" ? job.success : null,
  state:stage==="history" ? job.previousError ? "failed" : "succeeded" :
    stage==="success" ? "succeeded" : stage==="failure" ? "failed" : "running",
  last_attempt_at:stage==="history" ? job.previousAttempt : job.attempt,
  last_success_at:stage==="success" ? job.success : job.previousSuccess,
  last_error:stage==="failure" ? "INGESTION_ERROR" : stage==="history" ? job.previousError : null,
  items_seen:stage==="success" ? 2 : 0,items_created:stage==="success" ? 1 : 0,
  items_updated:0,items_unchanged:stage==="success" ? 1 : 0,
  items_failed:stage==="failure" ? 1 : 0,items_deleted:0,cursor:null});
window.fetch=async(url,init={})=>{
  const path=String(url),method=init.method||"GET",body=init.body?JSON.parse(init.body):null;
  state.calls.push({path,method,body});
  if(body!==null && (typeof body!=="object" || Array.isArray(body))) throw Error("double serialized settings");
  const ws=path.includes("/ws-2/") ? "ws-2" : "ws-1";
  const id=decodeURIComponent(path.split("/sources/")[1]?.split("/")[0]||"");
  const source=state.sources.find(item=>item.connector_id===id);
  if(path.endsWith("/sources") && method==="GET") {
    if(ws==="ws-1" && state.deferInitial) {
      state.deferInitial=false;
      return new Promise(resolve=>{state.releaseInitial=()=>resolve(json([]));});
    }
    return json(ws==="ws-1" ? state.sources : []);
  }
  if(path.endsWith("/sources") && method==="POST") {
    if(state.sources.some(item=>item.connector_id===body.connector_id)) return json({message:"duplicate connector"},409);
    const key={local_upload:"path",web:"urls",s3:"bucket",git:"repo"}[body.kind];
    const value=body.settings[key];
    if(!value || (key==="urls" ? !Array.isArray(value) : typeof value!=="string")) return json({message:"invalid settings"},422);
    const created={...body,credential_ref:null,last_attempt_at:initial,last_success_at:initial,last_error:null,created_at:initial,updated_at:initial};
    state.sources.push(created);return json(created,201);
  }
  if(path.endsWith("/status")) {
    if(!source) return json({message:"not found"},404);
    const job=state.jobs[id];
    if(!job) return json({connector_id:id,kind:source.kind,last_attempt_at:initial,last_success_at:initial,
      last_error:null,items_seen:0,items_created:0,items_updated:0,items_unchanged:0,items_failed:0,items_deleted:0,cursor:null});
    let stage=job.stage;
    if(stage==="pending") {
      job.polls++;
      stage=job.polls===1 ? "history" : job.polls===2 ? "running" : job.fail ? "failure" : "success";
      if(stage==="failure" || stage==="success") job.stage=stage;
    }
    const payload=statusPayload(id,job,stage);
    if(stage==="failure" || stage==="success") Object.assign(source,{last_attempt_at:payload.last_attempt_at,last_success_at:payload.last_success_at,last_error:payload.last_error});
    return json(payload);
  }
  if(path.endsWith("/sync")) {
    if(!source) return json({message:"not found"},404);
    const execute=()=>{
      const previous=state.jobs[id];const attempt=++state.counter;
      state.jobs[id]={attempt:new Date(Date.parse(initial)+attempt*10000).toISOString(),
        success:new Date(Date.parse(initial)+attempt*10000+1000).toISOString(),
        previousAttempt:source.last_attempt_at,previousSuccess:source.last_success_at,
        previousError:source.last_error,previousRun:previous?.run??null,run:"run-"+attempt,
        stage:"pending",polls:0,
        fail:id==="failsrc" && !previous};
      return json({job_id:"sync:"+ws+"/"+id,workspace_id:ws,connector_id:id,acked:true});
    };
    if(state.deferTrigger) {state.deferTrigger=false;return new Promise(resolve=>{state.pending=()=>resolve(execute());});}
    return execute();
  }
  if(method==="PATCH" && source) {source.settings=body.settings;return json(source);}
  if(method==="DELETE" && source) {state.sources=state.sources.filter(item=>item!==source);return new Response(null,{status:204});}
  return json({message:"unexpected fixture route"},404);
};
`;

const ENTRY = String.raw`
import React from "react";
import {createRoot} from "react-dom/client";
import SourcesPage from "@/app/sources/page";
import {WorkspaceProvider} from "@/components/workspace/workspace-provider";
const sleep=(ms=20)=>new Promise(resolve=>setTimeout(resolve,ms));
const text=()=>document.body.textContent||"";
async function waitFor(predicate,label) {
  for(let i=0;i<400;i++){if(predicate())return;await sleep();}
  throw Error("timed out: "+label);
}
async function fill(placeholder,value) {
  const node=document.querySelector('input[placeholder="'+placeholder+'"]');
  if(!node)throw Error("missing input "+placeholder);
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(node,value);
  node.dispatchEvent(new Event("input",{bubbles:true}));await sleep();
}
function click(label) {
  const button=Array.from(document.querySelectorAll("button")).find(node=>node.getAttribute("aria-label")===label);
  if(!button)throw Error("missing button "+label);button.click();
}
const row=id=>document.querySelector('[data-testid="source-'+id+'"]');
async function create(id,kind,placeholder,value) {
  await fill("connector id",id);
  const select=document.querySelector('select');select.value=kind;
  select.dispatchEvent(new Event("change",{bubbles:true}));await sleep();
  await fill(placeholder,value);click("add connector");
  await waitFor(()=>row(id),"create "+id);
}
const outcomes={};
(async()=>{
  createRoot(document.getElementById("app")).render(<WorkspaceProvider><SourcesPage/></WorkspaceProvider>);
  await waitFor(()=>window.fixture.calls.length>0 && text().includes("No connectors."),"empty state");
  outcomes.empty=true;
  for(const args of [["src-local","local_upload","path","/approved/docs"],
    ["src-web","web","urls (comma separated)","https://example.com/docs"],
    ["src-s3","s3","bucket","123"],["src-git","git","repo","https://example.com/docs.git"]]) await create(...args);
  outcomes.created=document.querySelectorAll('[data-testid^="source-"]').length===4;
  window.fixture.releaseInitial();await sleep(100);
  outcomes.staleList=document.querySelectorAll('[data-testid^="source-"]').length===4;
  outcomes.requestBodies=window.fixture.calls.filter(call=>call.method==="POST").length===4 &&
    window.fixture.sources.find(item=>item.connector_id==="src-s3").settings.bucket==="123" &&
    Array.isArray(window.fixture.sources.find(item=>item.connector_id==="src-web").settings.urls);
  click("edit src-s3");await sleep();await fill("bucket","true");
  await fill("prefix","[2026]");click("save connector");
  await waitFor(()=>row("src-s3").textContent.includes('"bucket":"true"'),"edited readback");
  const patch=window.fixture.calls.find(call=>call.method==="PATCH").body.settings;
  outcomes.edited=patch.bucket==="true" && patch.prefix==="[2026]";
  click("sync src-s3");
  await waitFor(()=>window.fixture.jobs["src-s3"]?.stage==="success","sync completes beyond historical success");
  await waitFor(()=>row("src-s3").textContent.includes("created 1"),"progress counters");
  outcomes.synced=window.fixture.jobs["src-s3"].polls===3;
  await create("failsrc","s3","bucket","fixture-bucket");
  click("sync failsrc");
  await waitFor(()=>row("failsrc").textContent.includes("INGESTION_ERROR"),"visible failure");
  outcomes.failed=!!row("failsrc").querySelector('button[aria-label="retry failsrc"]');
  click("retry failsrc");
  await waitFor(()=>window.fixture.jobs.failsrc.stage==="success","retry completes");
  await waitFor(()=>!row("failsrc").textContent.includes("INGESTION_ERROR"),"retry clears error");
  outcomes.retried=window.fixture.calls.filter(call=>call.path.endsWith("/failsrc/sync")).length===2;
  click("delete src-git");await waitFor(()=>!row("src-git"),"deleted readback");outcomes.deleted=true;

  // Resolve an accepted trigger after the old workspace child has unmounted.
  window.fixture.deferTrigger=true;click("sync src-s3");
  await waitFor(()=>window.fixture.pending,"deferred trigger");
  window.setWorkspace("ws-2");await waitFor(()=>text().includes("No connectors."),"B list");
  outcomes.workspaceFence=!text().includes("src-s3") && !text().includes("INGESTION_ERROR");
  window.setWorkspace("ws-1");await waitFor(()=>row("src-s3"),"new A child");
  const before=window.fixture.calls.filter(call=>call.path.endsWith("/status")).length;
  window.fixture.pending();await sleep(2500);
  const after=window.fixture.calls.filter(call=>call.path.endsWith("/status")).length;
  outcomes.delayedNoZombie=after===before;
})().catch(error=>{outcomes.error=String(error?.stack||error);}).finally(()=>{
  const report=document.createElement("pre");report.id="report";
  report.textContent=JSON.stringify(outcomes);document.body.appendChild(report);
});
`;

let report: Record<string, boolean | string> = {};
describe.skipIf(!chrome)("sources page browser outcomes", () => {
  let directory = "";
  beforeAll(async () => {
    directory = mkdtempSync(join(tmpdir(), "sources-browser-"));
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
  for (const outcome of ["empty","created","staleList","requestBodies","edited","synced","failed",
    "retried","deleted","workspaceFence","delayedNoZombie"]) {
    it(outcome, () => {expect(report[outcome]).toBe(true);});
  }
});
