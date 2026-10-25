/**
 * M5-001a REV-3 (police finding 4) — OUTCOME regression for turn-scoped
 * streaming, driven through the REAL component tree in a REAL browser.
 *
 * The finding: a workspace switch mid-generation let workspace A's tokens
 * paint over workspace B's last historical assistant message (the streaming
 * surface was one global string). The rework scoped the surface to the
 * originating turn; this file drives the real page with the real React
 * scheduling and the interleavings the police probe described:
 *
 *   1. a delayed A token released DURING B's layout commit (a frame already
 *      in flight when the switch happened) must not paint under B — the
 *      render-level scope gate;
 *   2. A finishing AFTER B has begun streaming must not clear B's surface or
 *      B's pending indicator (the adjacent REV-3 class: ungated exit paths);
 *   3. the switch must still TEAR DOWN A's stream, and leave no stuck
 *      "streaming…" indicator;
 *   4. control: B's own tokens render and B's citations finalize; a request B
 *      starts during B's own commit window survives A's late completion.
 *
 * The controlled stream stub yields already-released tokens BEFORE honoring
 * the abort — that ordering is what models a frame in flight at the moment of
 * the switch (a stream that only sees the abort would never deliver it).
 *
 * The page under test can be pointed at a modeled pre-fix page with
 * `ASK_PAGE_PATH` — that is how the teeth are demonstrated (the same legs
 * fail against the unfenced page).
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
// apps/web (HERE is apps/web/src/app/ask/)
const WEB_ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const PAGE_PATH =
  process.env.ASK_PAGE_PATH && process.env.ASK_PAGE_PATH.length > 0
    ? process.env.ASK_PAGE_PATH
    : join(HERE, "page.tsx");
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
  `,
  "./stream": `
    // Controlled SSE client: the test pushes tokens and ends streams through
    // window.streams. Released tokens are yielded BEFORE the abort is honored
    // (a frame in flight when the workspace switched), and a stream can be
    // pinned to OUTLIVE its abort (allowAbortExit=false) to model a lagging
    // teardown. The generator's finally records how the stream ended.
    export const streamQuestion = (workspaceId, query, model, signal, turnId) => {
      const handle = { workspaceId, turnId, tokens: [], ended: false, allowAbortExit: true, errored: false };
      window.streams.push(handle);
      return (async function* () {
        try {
          for (;;) {
            if (handle.tokens.length) {
              yield { type: "token", text: handle.tokens.shift(), turnId };
            } else if (handle.errored) {
              // A terminal error frame (e.g. the 403/503 ACL/LLM failure the
              // backend emits as SSE). window.omitErrorTurnId models the
              // PRE-FIX client, which yielded this frame WITHOUT the turnId —
              // the page's guard then dropped it and the placeholder stayed
              // blank (the REV-3 residual POLICE probed). The yielded frame is
              // recorded so the leg can prove WHICH shape was delivered.
              const event = { type: "error", code: "ACL_DENIED", message: "not allowed" };
              if (!window.omitErrorTurnId) event.turnId = turnId;
              window.errorEvents.push({
                hadTurnId: Object.hasOwn(event, "turnId"),
                omit: window.omitErrorTurnId === true,
              });
              yield event;
              return;
            } else if (handle.ended) {
              yield {
                type: "citations",
                answer: { answer: "answered by " + workspaceId, status: "answered", claims: [], citations: [] },
                turnId,
              };
              return;
            } else if (signal && signal.aborted && handle.allowAbortExit) {
              return;
            } else {
              await new Promise((r) => setTimeout(r, 10));
            }
          }
        } finally {
          window.endedStreams.push({
            workspaceId,
            turnId,
            aborted: signal ? signal.aborted === true : false,
          });
        }
      })();
    };
  `,
  "./components/chat-window": `
    export const userMessage = (q) => ({ role: "user", content: q });
    export const answerMessage = (answer) => ({
      role: "assistant",
      content: answer.answer,
      claims: [],
      citations: [],
      status: answer.status,
    });
    export const failureMessage = (error) => ({
      role: "assistant",
      content: "Error: " + String(error),
      error: true,
    });
    export const ChatWindow = ({ pending, messages, streamingContent, onSend }) => (
      <div>
        <div id="pending">{pending ? "PENDING" : "IDLE"}</div>
        <div id="streaming">{streamingContent}</div>
        <div id="messages">
          {messages.map((m, i) => (
            <div key={i} data-role={m.role}>{m.content}</div>
          ))}
        </div>
        <form
          id="composer"
          onSubmit={(e) => {
            e.preventDefault();
            const input = e.currentTarget.querySelector("input");
            onSend(input.value);
          }}
        >
          <input aria-label="q" />
          <button type="submit">send</button>
        </form>
      </div>
    );
  `,
  "./components/source-panel": `
    export const SourcePanel = () => null;
    export const parseSourceRef = () => null;
    export const serializeSourceRef = () => "";
  `,
  "./components/model-selector": `export const ModelSelector = () => null;`,
};

const ENTRY = `
import React, { useEffect, useLayoutEffect } from "react";
import { createRoot } from "react-dom/client";
import Page from ${JSON.stringify(PAGE_PATH)};
import { WorkspaceProvider, useWorkspaces } from ${JSON.stringify(PROVIDER_PATH)};

// The page probes the account scope on mount — answer before any render.
window.fetch = () => Promise.resolve({ ok: true, json: async () => ({ scope: "acct" }) });

window.trace = [];
window.streams = [];
window.endedStreams = [];
window.errorEvents = [];
window.workspaceList = undefined;
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
    // The commit window: after the render that switches to B, before B's
    // passive effects run. Release A's delayed token HERE — exactly when a
    // real in-flight SSE frame can still land.
    if (id === "B" && window.releaseAt === "layout") {
      const aStream = window.streams.find((s) => s.workspaceId === "A");
      if (aStream) {
        aStream.tokens.push("A-LATE-TOKEN");
        window.trace.push("released A token at B layout");
      }
    }
    // Start B's own request during B's layout commit: it belongs to the
    // active workspace and must survive A's late completion.
    if (id === "B" && window.startBAtLayout) {
      document.querySelector("#composer")
        .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      window.trace.push("submitted B in B layout");
    }
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

async function submit(value) {
  const input = document.querySelector("input");
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  setter.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  await tick();
  document.querySelector("#composer")
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await tick();
}

const streamFor = (workspaceId) =>
  window.streams.find((s) => s.workspaceId === workspaceId && !s.ended);

async function freshMount() {
  window.trace = [];
  window.streams = [];
  window.endedStreams = [];
  window.errorEvents = [];
  window.workspaceList = undefined;
  window.releaseAt = "none";
  window.startBAtLayout = false;
  window.omitErrorTurnId = false;
  // The page PERSISTS its transcript per scope (localStorage) and hydrates on
  // mount — without this clear a fresh leg would inherit the previous leg's
  // saved messages and assertions would see stale content.
  window.localStorage.clear();
  root.render(<Harness key={Math.random()} />);
  await tick();
  await tick();
}

function snapshot(caseName) {
  return {
    case: caseName,
    pending: host.querySelector("#pending").textContent,
    streaming: host.querySelector("#streaming").textContent,
    messages: [...host.querySelectorAll("#messages [data-role]")].map((n) => n.textContent),
    ended: [...window.endedStreams],
    errorEvents: [...window.errorEvents],
    trace: [...window.trace],
  };
}

(async () => {
  try {
    const legs = [];

    // 1. A's delayed token released in B's commit window.
    await freshMount();
    window.releaseAt = "layout";
    await submit("salary");
    if (!streamFor("A")) throw new Error("leg1: no A stream");
    streamFor("A").tokens.push("A-TOKEN-1");
    await tick();
    await window.switchTo();
    await tick();
    await tick();
    legs.push(snapshot("delayed token in B commit window"));

    // 2. A finishes AFTER B has begun streaming (lagging teardown).
    await freshMount();
    await submit("salary");
    streamFor("A").tokens.push("A-TOKEN-1");
    await tick();
    streamFor("A").allowAbortExit = false; // A outlives its abort
    await window.switchTo();
    await tick();
    await submit("revenue");            // B begins streaming
    await tick();
    const bStream = streamFor("B");
    if (!bStream) throw new Error("leg2: no B stream");
    bStream.tokens.push("B-TOKEN-1");
    await tick();
    streamFor("A").ended = true;        // A's completion lands late
    await tick();
    await tick();
    legs.push(snapshot("A ends after B begins"));

    // 3. Switch with NO B send: no stuck pending indicator, A torn down.
    await freshMount();
    await submit("salary");
    streamFor("A").tokens.push("A-TOKEN-1");
    await tick();
    await window.switchTo();
    await tick();
    await tick();
    legs.push(snapshot("no stuck pending after switch"));

    // 4. B starts during B's own commit window, then A ends late.
    await freshMount();
    await submit("salary");
    streamFor("A").tokens.push("A-TOKEN-1");
    await tick();
    streamFor("A").allowAbortExit = false;
    window.startBAtLayout = true;
    await window.switchTo();
    await tick();
    const bInLayout = streamFor("B");
    if (!bInLayout) throw new Error("leg4: no B stream started in B layout");
    bInLayout.tokens.push("B-TOKEN-1");
    await tick();
    streamFor("A").ended = true;
    await tick();
    await tick();
    legs.push(snapshot("B in own commit window, A ends late"));

    // 5. A terminal error frame (403/503-style) is DISPLAYED by the page —
    //    the client stamps the caller's turnId so the page's turn guard
    //    accepts it (REV-3 residual: without the turnId the frame is dropped
    //    and the placeholder assistant stays blank).
    await freshMount();
    await submit("salary");
    streamFor("A").errored = true;
    await tick();
    await tick();
    legs.push(snapshot("error frame with turn id is displayed"));

    // 6. The same frame WITHOUT the turnId (pre-fix client): the page's guard
    //    drops it — the placeholder stays blank. This is the failure mode the
    //    REV-3 residual fix closes, pinned so it cannot pass silently.
    await freshMount();
    window.omitErrorTurnId = true;
    await submit("salary");
    streamFor("A").errored = true;
    await tick();
    await tick();
    legs.push(snapshot("unattributed error frame leaves blank placeholder"));
    window.omitErrorTurnId = false;

    // 7. Control: ordinary B flow finalizes from the citations event.
    await freshMount();
    await submit("salary");
    streamFor("A").tokens.push("A-TOKEN-1");
    await tick();
    await window.switchTo();
    await tick();
    await submit("revenue");
    await tick();
    const bFresh = streamFor("B");
    if (!bFresh) throw new Error("leg5: no B stream");
    bFresh.tokens.push("B-TOKEN-1");
    await tick();
    // Mid-flight snapshot: the surface and the pending state belong to B.
    legs.push(snapshot("fresh B mid-flight"));
    bFresh.ended = true;
    await tick();
    await tick();
    legs.push(snapshot("fresh B finalizes"));

    report.textContent = JSON.stringify(legs);
  } catch (error) {
    report.textContent = JSON.stringify({ error: String(error), trace: window.trace });
  }
})();
`;

const stubPlugin: Plugin = {
  name: "ask-outcome-stubs",
  setup(build) {
    build.onResolve({ filter: /.*/ }, (args) =>
      Object.hasOwn(STUBS, args.path)
        ? { path: args.path, namespace: "ask-outcome-stub" }
        : null,
    );
    build.onLoad({ filter: /.*/, namespace: "ask-outcome-stub" }, (args) => ({
      contents: STUBS[args.path],
      loader: "tsx",
      resolveDir: WEB_ROOT,
    }));
  },
};

let browserReport: Leg[] | { error: string; trace?: string[] } | null = null;

type Leg = {
  case: string;
  pending?: string;
  streaming?: string;
  messages?: string[];
  ended?: { workspaceId: string; turnId?: string; aborted: boolean }[];
  errorEvents?: { hadTurnId: boolean; omit: boolean }[];
  trace?: string[];
  error?: string;
};

function legs(): Leg[] {
  if (!Array.isArray(browserReport)) return [];
  return browserReport;
}

function legFor(name: string): Leg {
  const found = legs().find((r) => r.case === name);
  if (!found) throw new Error(`missing browser leg: ${name}`);
  return found;
}

describe.skipIf(!chrome)("M5-001a REV-3: turn-scoped streaming holds in the real commit window", () => {
  let workdir = "";

  beforeAll(async () => {
    workdir = mkdtempSync(join(tmpdir(), "ask-outcome-"));
    const esbuild = await import("esbuild");
    const outfile = join(workdir, "bundle.js");
    await esbuild.build({
      stdin: {
        contents: ENTRY,
        resolveDir: WEB_ROOT,
        loader: "tsx",
        sourcefile: "ask-outcome-entry.tsx",
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

  it("never paints a delayed A token under B when it lands in B's commit window", () => {
    const leg = legFor("delayed token in B commit window");
    expect(leg.trace).toContain("released A token at B layout");
    // The late A token never reaches B's streaming surface…
    expect(leg.streaming).not.toContain("A-LATE-TOKEN");
    // …and never reaches B's transcript either.
    for (const message of leg.messages ?? []) {
      expect(message).not.toContain("A-LATE-TOKEN");
    }
  });

  it("A finishing after B begins does not clear B's surface or pending state", () => {
    const leg = legFor("A ends after B begins");
    expect(leg.streaming).toContain("B-TOKEN-1");
    expect(leg.pending).toBe("PENDING"); // B's turn is still streaming
    for (const message of leg.messages ?? []) {
      expect(message).not.toContain("answered by A");
    }
  });

  it("a workspace switch leaves no stuck pending indicator", () => {
    const leg = legFor("no stuck pending after switch");
    expect(leg.pending).toBe("IDLE");
    expect(leg.streaming).toBe("");
  });

  it("tears A's stream down on the scope change", () => {
    const ended = legFor("no stuck pending after switch").ended ?? [];
    const aEnd = ended.find((e) => e.workspaceId === "A");
    expect(aEnd).toBeDefined();
    expect(aEnd?.aborted).toBe(true);
  });

  it("keeps a request B starts during its own commit window when A ends late", () => {
    const leg = legFor("B in own commit window, A ends late");
    expect(leg.trace).toContain("submitted B in B layout");
    // B's own token paints…
    expect(leg.streaming).toContain("B-TOKEN-1");
    // …and A's late completion never bleeds into it.
    expect(leg.streaming).not.toContain("A-");
  });

  it("displays a terminal error frame the page's turn guard accepts", () => {
    const leg = legFor("error frame with turn id is displayed");
    // The client delivered the frame WITH the caller's turnId…
    expect(leg.errorEvents).toEqual([{ hadTurnId: true, omit: false }]);
    // …and the page displayed the failure copy — not a blank assistant message.
    expect(leg.messages?.some((m) => m === "Error: not allowed")).toBe(true);
    expect(leg.pending).toBe("IDLE");
  });

  it("pins the pre-fix failure mode: an unattributed error frame leaves a blank placeholder", () => {
    const leg = legFor("unattributed error frame leaves blank placeholder");
    // Diagnostic leg: prove WHICH frame shape the client delivered — a frame
    // genuinely WITHOUT the turnId — before asserting the guard's outcome.
    expect(leg.errorEvents).toEqual([{ hadTurnId: false, omit: true }]);
    // The page's turn guard drops it: no displayed error, placeholder blank.
    expect(leg.messages?.some((m) => m === "Error: not allowed")).toBe(false);
  });

  it("control: a fresh B turn streams and finalizes from its citations event", () => {
    const midFlight = legFor("fresh B mid-flight");
    expect(midFlight.streaming).toContain("B-TOKEN-1");
    expect(midFlight.pending).toBe("PENDING");
    const leg = legFor("fresh B finalizes");
    expect(leg.pending).toBe("IDLE"); // finalized by the citations event
    expect(leg.messages?.some((m) => m === "answered by B")).toBe(true);
  });
});
