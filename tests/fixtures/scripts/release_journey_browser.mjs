// Live M2/M5 acceptance. Only HTTP observations and actual UI interactions;
// no route mocks, API keys, model stubs, or fabricated document bytes.
// QA dependency: Playwright, resolved through PLAYWRIGHT_MODULE or the package.
import { readFileSync, statSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { armAskStreamObserver, installAskStreamObserver, readObservedAskStream } from "./browser_sse_observer.mjs";

const base = (process.env.BASE_URL || "http://127.0.0.1:3001").replace(/\/$/, "");
const workspaceId = process.env.WORKSPACE_ID;
const credentialsPath = process.env.CREDENTIAL_FILE;
const timeout = Number(process.env.RELEASE_TIMEOUT_MS || 180000);
const fixtures = new URL("../pd001/", import.meta.url);
const formats = [
  ["pdf_text_multipage.pdf", "application/pdf"],
  ["docx_headings_table.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
  ["html_article_nav.html", "text/html"], ["csv_multilingual.csv", "text/csv"],
  ["markdown_structure.md", "text/markdown"], ["txt_en.txt", "text/plain"],
];
const run = randomUUID().slice(0, 12);
const uploaded = [];
const summary = { formats_ready: 0, search_modes: [], citation_passage: false, keyboard: false };
let stage = "configuration", browser, context, failure;
class GateError extends Error { constructor(code) { super(code); this.code = code; } }
const check = (condition, code) => { if (!condition) throw new GateError(code); };
const backend = path => `${base}/api/backend/v1/workspaces/${workspaceId}${path}`;

function observeResponse(page, predicate) {
  // Attach rejection handling before the UI action: an early action failure
  // must not leave a timer that later emits a Playwright call log.
  const pending = page.waitForResponse(predicate, { timeout }).then(
    response => ({ response }), error => ({ error }));
  return async () => {
    const result = await pending;
    check(!result.error, "response_observation_failed");
    return result.response;
  };
}

function finalAnswer(wire) {
  let answer;
  for (const frame of wire.split(/\r?\n\r?\n/)) {
    const lines = frame.split(/\r?\n/);
    const event = lines.find(line => line.startsWith("event:"))?.slice(6).trim();
    const data = lines.filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
    if (event === "error") throw new GateError("answer_stream_error");
    if (event === "citations") answer = JSON.parse(data);
  }
  check(answer, "terminal_citations_missing");
  return answer;
}

try {
  check(workspaceId && credentialsPath, "WORKSPACE_ID_and_CREDENTIAL_FILE_required");
  check(Number.isFinite(timeout) && timeout > 0 && timeout <= 900000, "invalid_timeout");
  check((statSync(credentialsPath).mode & 0o077) === 0, "credential_file_must_be_private");
  const credentials = JSON.parse(readFileSync(credentialsPath, "utf8"));
  check(typeof credentials.email === "string" && typeof credentials.password === "string",
    "credential_file_requires_email_and_password");
  const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
  browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH || undefined });
  context = await browser.newContext();
  context.setDefaultTimeout(timeout);
  const page = await context.newPage();
  stage = "observer_install";
  await installAskStreamObserver(page, workspaceId, timeout);

  stage = "login";
  await page.goto(`${base}/login`);
  await page.locator('input[type="email"]').fill(credentials.email);
  await page.locator('input[type="password"]').fill(credentials.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.waitForURL(url => url.pathname !== "/login", { timeout });

  stage = "workspace";
  const memberships = await context.request.get(`${base}/api/backend/v1/workspaces`);
  check(memberships.status() === 200, "workspace_memberships_unavailable");
  const workspaces = await memberships.json();
  const index = workspaces.findIndex(item => item.id === workspaceId);
  check(index >= 0, "workspace_not_in_account");
  check(["owner", "admin", "editor"].includes(workspaces[index].role), "workspace_cannot_upload");
  await page.goto(`${base}/knowledge`);
  await page.getByRole("button", { name: "Switch workspace", exact: true }).click();
  // Workspaces are the initial menu items, in the provider's API order. UUIDs
  // are not displayed and workspace names are not necessarily unique.
  await page.getByRole("menuitem").nth(index).click();
  await page.waitForFunction(id => localStorage.getItem("kios_active_ws") === id,
    workspaceId, { timeout });
  await page.goto(`${base}/knowledge/${workspaceId}`);

  for (const [filename, mimeType] of formats) {
    stage = `upload_${filename}`;
    const name = `release-${run}-${filename}`;
    let accepted;
    // Observe the ACTUAL UI poll for this upload's specific job; an earlier
    // ready row or a previous file's progress cannot satisfy this condition.
    const terminal = page.waitForResponse(async response => {
      if (!accepted || response.request().method() !== "GET" ||
          new URL(response.url()).pathname !== `/api/backend/v1/workspaces/${workspaceId}/jobs/${accepted.job_id}`) return false;
      if (response.status() !== 200) return true;
      const job = await response.json();
      return ["ready", "failed", "partial", "degraded", "needs_review"].includes(job.status);
    }, { timeout }).then(response => ({ response }), error => ({ error }));
    const submitted = observeResponse(page, response => response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/backend/v1/workspaces/${workspaceId}/documents`);
    await page.locator('input[type="file"]').setInputFiles({
      name, mimeType, buffer: readFileSync(new URL(filename, fixtures)),
    });
    const response = await submitted();
    check(response.status() === 202, "upload_not_accepted");
    accepted = await response.json();
    check(accepted.document_id && accepted.job_id, "upload_identity_missing");
    uploaded.push({ ...accepted, name });
    const finished = await terminal;
    check(!finished.error && finished.response?.status() === 200, "specific_job_poll_failed");
    const job = await finished.response.json();
    check(job.document_id === accepted.document_id && job.job_id === accepted.job_id,
      "job_identity_mismatch");
    check(job.status === "ready" && job.stage === "ready", "ingestion_not_ready");
    await page.waitForFunction(currentName => {
      const upload = document.querySelector('section[aria-label="Upload"]');
      return upload?.textContent.includes(currentName) &&
        upload.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow") === "100";
    }, name, { timeout });
    summary.formats_ready++;
  }

  stage = "ask_navigation";
  const textDocument = uploaded.find(item => item.name.endsWith("txt_en.txt"));
  await page.goto(`${base}/ask`);
  const question = "In Knowledge OS Text Fixture, quote exactly the sentence beginning 'This plain text document exercises'.";
  stage = "ask_observer_arm";
  check(await armAskStreamObserver(page, question), "answer_observer_not_installed");
  const answerResponse = observeResponse(page, response => response.request().method() === "POST" &&
    new URL(response.url()).pathname === `/api/backend/v1/workspaces/${workspaceId}/ask/stream` &&
    response.request().postDataJSON().query === question);
  stage = "ask_fill";
  await page.getByPlaceholder("Ask anything…").fill(question);
  stage = "ask_send";
  await page.getByRole("button", { name: "Send", exact: true }).click();
  stage = "ask_network_response";
  const stream = await answerResponse();
  summary.answer_http_status = stream.status();
  check(stream.status() === 200, "answer_http_failure");
  stage = "ask_observer_read";
  const observed = await readObservedAskStream(page, question, timeout);
  check(observed.ok, observed.code);
  check(observed.http_status === 200 && observed.request_count === 1, "answer_observer_http_mismatch");
  summary.answer_observation = { transport: "actual_browser_fetch_clone", bytes: observed.bytes,
    request_count: observed.request_count };
  stage = "ask_wire_parse";
  const answer = finalAnswer(observed.wire);
  stage = "ask_answer_assertions";
  summary.answer_status = /^[a-z_]{1,64}$/.test(answer.status) ? answer.status : "unknown";
  summary.publication_status = /^[a-z_]{1,64}$/.test(answer.publication_status) ? answer.publication_status : "unknown";
  summary.answer_reason_code = /^[A-Z_0-9]{1,64}$/.test(answer.reason_code) ? answer.reason_code : null;
  check(answer.status === "answered" && answer.publication_status === "verified", "answer_not_verified");
  const textCitation = answer.citations?.find(citation => citation.document_id === textDocument.document_id);
  check(textCitation?.chunk_id, "answer_did_not_cite_uploaded_text_document");
  const passageResponse = observeResponse(page, response => {
    const url = new URL(response.url());
    return response.request().method() === "GET" && url.pathname === "/api/passage" &&
      url.searchParams.get("workspace_id") === workspaceId &&
      url.searchParams.get("chunk_id") === textCitation.chunk_id;
  });
  stage = "citation_open";
  await page.locator('button[title*="' + textDocument.name + '"]').first().click();
  stage = "citation_network_response";
  const passage = await passageResponse();
  check(passage.status() === 200, "canonical_passage_http_failure");
  stage = "citation_body_read";
  const canonical = await passage.json();
  stage = "citation_identity";
  check(canonical.ok === true && canonical.passage?.chunk_id === textCitation.chunk_id &&
    canonical.passage?.document_id === textDocument.document_id &&
    canonical.passage?.content?.includes("TXT parser and language tagging"), "canonical_passage_mismatch");
  const panel = page.locator('aside:has(button[aria-label="Close source panel"])');
  stage = "citation_panel_visible";
  await panel.waitFor({ state: "visible", timeout });
  stage = "citation_canonical_live_badge";
  await panel.getByText("Canonical (live)", { exact: true }).waitFor({ state: "visible", timeout });
  stage = "citation_passage_visible";
  await page.waitForFunction(name => {
    const panel = document.querySelector('aside:has(button[aria-label="Close source panel"])');
    const text = panel?.textContent.replace(/\s+/g, " ") || "";
    return text.includes(name) && text.includes("TXT parser and language tagging");
  }, textDocument.name, { timeout });
  summary.citation_passage = true;

  for (const mode of ["hybrid", "semantic", "keyword"]) {
    stage = `search_${mode}`;
    await page.goto(`${base}/search`);
    // Choose a mode before entering the query, avoiding a second auto-submit.
    await page.getByRole("radio", { name: new RegExp(`^${mode}`, "i") }).click();
    await page.getByRole("textbox", { name: "Search query", exact: true }).fill("TXT parser language tagging");
    const searched = observeResponse(page, response => {
      if (response.request().method() !== "POST" ||
          new URL(response.url()).pathname !== `/api/backend/v1/workspaces/${workspaceId}/search`) return false;
      const body = response.request().postDataJSON();
      return body.mode === mode && body.query === "TXT parser language tagging";
    });
    await page.getByRole("button", { name: "Search", exact: true }).click();
    const response = await searched();
    check(response.status() === 200, "search_http_failure");
    const results = await response.json();
    check(Array.isArray(results.results) && results.results.some(item => item.document_id === textDocument.document_id),
      "search_missing_uploaded_document");
    await page.waitForFunction(([name, id]) => {
      const main = document.querySelector("main");
      return main?.textContent.includes(name) || main?.textContent.includes(id);
    }, [textDocument.name, textDocument.document_id], { timeout });
    summary.search_modes.push(mode);
  }

  stage = "keyboard_and_announcements";
  await page.goto(`${base}/ask`);
  const model = page.getByRole("button", { name: "Select model", exact: true });
  await model.focus();
  await model.press("ArrowDown");
  await page.waitForFunction(() => document.activeElement?.getAttribute("role") === "option", null, { timeout });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "Select model", null, { timeout });
  check(await page.locator('main [aria-live="polite"]').count() > 0, "polite_announcements_missing");
  summary.keyboard = true;
} catch (error) {
  // Playwright exception messages can contain fill() passwords or HTTP bodies.
  // Emit only our stage and exception type, never its message/call log.
  failure = { stage, error_type: error?.name || "Error",
    ...(error instanceof GateError ? { code: error.code } : {}) };
} finally {
  if (context) {
    for (const document of uploaded) {
      try {
        const removed = await context.request.delete(backend(`/documents/${document.document_id}`));
        if (removed.status() !== 204) failure ??= { stage: "fixture_cleanup", error_type: "DeleteFailed" };
      } catch {
        failure ??= { stage: "fixture_cleanup", error_type: "DeleteFailed" };
      }
    }
  }
  if (browser) {
    try { await browser.close(); }
    catch { failure ??= { stage: "browser_cleanup", error_type: "CloseFailed" }; }
  }
}
if (process.env.RELEASE_REPORT_FILE) writeFileSync(process.env.RELEASE_REPORT_FILE,
  JSON.stringify({ ...summary, failure }, null, 2), { mode: 0o600 });
if (failure) {
  console.error("RELEASE_JOURNEY_BROWSER_FAIL", failure.stage, failure.error_type);
  process.exitCode = 1;
} else {
  console.log("RELEASE_JOURNEY_BROWSER_OK", JSON.stringify(summary));
}
