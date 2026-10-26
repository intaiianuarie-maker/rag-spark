// Actual M7/M8 acceptance against a running release. No mocks or supplied
// retrieval traces. Uses the COMPLETE restored original golden/multilingual
// files (2 + 3 questions), public fixture contracts, five answer calls and ten
// retrieval-only evaluations (UI + independent production CLI).
//
// Environment: existing release_journey BASE_URL/WORKSPACE_ID/CREDENTIAL_FILE/
// PLAYWRIGHT_MODULE/CHROME_PATH/RELEASE_TIMEOUT_MS, plus MULTILINGUAL_REPORT_FILE
// and optional LIVE_EVAL_PYTHON (default python3 in the release API environment).
// LIVE_EVAL_PROVIDER_KEY_FILE optionally names a private JSON file read ONLY
// inside the Python backend CLI; no provider key is read into Node/browser env.
// Run remotely with the same DB/Qdrant/embedding/reranker environment as API.
// Exit 1 on ANY failed gate; individual outcomes remain separate in the report.
import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { armAskStreamObserver, installAskStreamObserver, readObservedAskStream } from "./browser_sse_observer.mjs";

const execute = promisify(execFile);
const base = (process.env.BASE_URL || "http://127.0.0.1:3001").replace(/\/$/, "");
const workspaceId = process.env.WORKSPACE_ID;
const timeout = Number(process.env.RELEASE_TIMEOUT_MS || 180000);
const reportPath = process.env.MULTILINGUAL_REPORT_FILE;
const root = fileURLToPath(new URL("../../../", import.meta.url));
const runId = randomUUID().slice(0, 12);
const uploads = [], answers = [], evaluations = [];
const report = { run_id: runId, corpus: "restored original sample-contract-en/ro/pt, one mixed workspace",
  datasets: "complete original golden.jsonl (2) and multilingual.jsonl (3)",
  scoring: "production retrieval metrics; lexical_overlap_v1 faithfulness proxy, no RAGAS claim",
  outcomes: {}, failures: [], trace_checks: [], answer_observations: [], cli: null };
let stage = "configuration", browser, context, temporary;
class GateError extends Error { constructor(code) { super(code); this.code = code; } }
const check = (condition, code) => { if (!condition) throw new GateError(code); };
const finite = value => typeof value === "number" && Number.isFinite(value);
const endpoint = path => `${base}/api/backend/v1/workspaces/${workspaceId}${path}`;
const jsonl = path => readFileSync(join(root, path), "utf8").split(/\r?\n/).filter(Boolean).map(JSON.parse);

function failure(name, error) {
  report.outcomes[name] = false;
  report.failures.push({ gate: name, stage,
    code: error instanceof GateError ? error.code : "operation_failed",
    error_type: error?.name || "Error" });
}
function observe(page, predicate) {
  const pending = page.waitForResponse(predicate, { timeout }).then(
    response => ({ response }), () => ({ error: true }));
  return async () => {
    const result = await pending;
    check(!result.error, "response_observation_failed");
    return result.response;
  };
}
function terminalAnswer(wire) {
  let answer;
  for (const frame of wire.split(/\r?\n\r?\n/)) {
    const lines = frame.split(/\r?\n/);
    const event = lines.find(line => line.startsWith("event:"))?.slice(6).trim();
    const data = lines.filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
    check(event !== "error", "answer_stream_error");
    if (event === "citations") answer = JSON.parse(data);
  }
  check(answer, "terminal_citations_missing");
  return answer;
}
function traceGaps(trace, question) {
  const missing = [];
  const queryHash = createHash("sha256").update(question).digest("hex");
  if (trace?.query?.sha256 !== queryHash || trace?.query?.characters !== Array.from(question).length) missing.push("query");
  if (!["unchanged", "rewritten"].includes(trace?.rewrite?.status) ||
      !/^[a-f0-9]{64}$/.test(trace?.rewrite?.forwarded_query_sha256 || "") ||
      !finite(trace?.rewrite?.forwarded_query_characters) ||
      (trace?.rewrite?.status === "unchanged" && trace.rewrite.forwarded_query_sha256 !== queryHash)) missing.push("rewrite");
  if (!trace?.filters?.workspace_ids?.includes(workspaceId) || trace.filters.include_public !== false) missing.push("filters");
  const items = trace?.retrieval?.items;
  if (!Array.isArray(items) || !items.length || !finite(trace?.retrieval?.latency_ms)) missing.push("retrieval");
  for (const score of ["dense", "lexical", "rrf", "rerank"]) {
    if (!items?.some(item => finite(item.scores?.[score]))) missing.push(`score_${score}`);
  }
  if (!trace?.context?.chunk_ids?.length || trace.context.source_text_included !== false) missing.push("context");
  if (!trace?.generation?.model || !trace.generation.prompt_template_sha256 || !finite(trace.generation.latency_ms)) missing.push("model_prompt_latency");
  if (!finite(trace?.generation?.prompt_tokens) || !finite(trace?.generation?.completion_tokens) ||
      trace?.generation?.usage_status !== "reported") missing.push("generation_tokens");
  if (!trace?.verification?.status || !finite(trace.verification.latency_ms)) missing.push("verification");
  if (!finite(trace?.total_latency_ms)) missing.push("total_latency");
  return missing;
}
function traceEvidence(trace) {
  if (!trace) return null;
  // Keep reviewable measurements, never arbitrary provider metadata or text.
  return {
    query: trace.query, rewrite: trace.rewrite, filters: trace.filters,
    retrieval: { status: trace.retrieval?.status, latency_ms: trace.retrieval?.latency_ms,
      candidate_scope: trace.retrieval?.candidate_scope,
      items: trace.retrieval?.items?.map(item => ({ chunk_id: item.chunk_id,
        document_id: item.document_id, workspace_id: item.workspace_id, score: item.score,
        scores: Object.fromEntries(["dense", "sparse", "lexical", "rrf", "rerank"]
          .filter(key => finite(item.scores?.[key])).map(key => [key, item.scores[key]])) })) },
    context: trace.context,
    generation: Object.fromEntries(["status", "model", "requested_model", "model_status",
      "prompt_template_sha256", "latency_ms", "prompt_tokens", "completion_tokens",
      "total_tokens", "cost_usd", "usage_status"].map(key => [key, trace.generation?.[key]])),
    verification: trace.verification, total_latency_ms: trace.total_latency_ms,
  };
}

try {
  check(workspaceId && process.env.CREDENTIAL_FILE && reportPath, "workspace_credentials_report_required");
  check(Number.isFinite(timeout) && timeout > 0 && timeout <= 900000, "invalid_timeout");
  check((statSync(process.env.CREDENTIAL_FILE).mode & 0o077) === 0, "credential_file_must_be_private");
  const credentials = JSON.parse(readFileSync(process.env.CREDENTIAL_FILE, "utf8"));
  check(typeof credentials.email === "string" && typeof credentials.password === "string", "invalid_credentials_file");
  const suites = { golden: jsonl("evals/golden.jsonl"), multilingual: jsonl("evals/multilingual.jsonl") };
  check(suites.golden.length === 2 && suites.multilingual.length === 3, "original_dataset_cardinality_changed");
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
  const membershipResponse = await context.request.get(`${base}/api/backend/v1/workspaces`);
  check(membershipResponse.status() === 200, "workspace_memberships_unavailable");
  const memberships = await membershipResponse.json();
  const index = memberships.findIndex(item => item.id === workspaceId);
  check(index >= 0 && ["owner", "admin", "editor"].includes(memberships[index].role), "workspace_cannot_upload");
  await page.goto(`${base}/knowledge`);
  await page.getByRole("button", { name: "Switch workspace", exact: true }).click();
  await page.getByRole("menuitem").nth(index).click();
  await page.waitForFunction(id => localStorage.getItem("kios_active_ws") === id, workspaceId, { timeout });
  await page.goto(`${base}/knowledge/${workspaceId}`);

  for (const language of ["en", "ro", "pt"]) {
    stage = `upload_${language}`;
    const filename = `sample-contract-${language}.txt`, name = `multilingual-${runId}-${filename}`;
    const submitted = observe(page, response => response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/backend/v1/workspaces/${workspaceId}/documents`);
    await page.locator('input[type="file"]').setInputFiles({ name, mimeType: "text/plain",
      buffer: readFileSync(join(root, "tests/fixtures", filename)) });
    const acceptedResponse = await submitted();
    check(acceptedResponse.status() === 202, "upload_not_accepted");
    const accepted = await acceptedResponse.json();
    check(accepted.document_id && accepted.job_id, "upload_identity_missing");
    uploads.push({ ...accepted, language, filename, name });
    // Observe actual UI job state as well as a scoped API read. UI completion
    // cannot be satisfied by another document because filename includes run ID.
    await page.waitForFunction(currentName => {
      const panel = document.querySelector('section[aria-label="Upload"]');
      return panel?.textContent.includes(currentName) && panel.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow") === "100";
    }, name, { timeout });
    const jobResponse = await context.request.get(endpoint(`/jobs/${accepted.job_id}`));
    check(jobResponse.status() === 200, "uploaded_job_missing");
    const job = await jobResponse.json();
    check(job.job_id === accepted.job_id && job.document_id === accepted.document_id &&
      job.status === "ready" && job.stage === "ready", "ingestion_not_ready");
  }
  report.outcomes.uploads = true;

  for (const item of [...suites.golden, ...suites.multilingual]) {
    stage = `answer_${item.id}_navigation`;
    try {
      await page.goto(`${base}/ask`);
      stage = `answer_${item.id}_observer_arm`;
      check(await armAskStreamObserver(page, item.question), "answer_observer_not_installed");
      const answered = observe(page, response => response.request().method() === "POST" &&
        new URL(response.url()).pathname === `/api/backend/v1/workspaces/${workspaceId}/ask/stream` &&
        response.request().postDataJSON().query === item.question);
      stage = `answer_${item.id}_fill`;
      await page.getByPlaceholder("Ask anything…").fill(item.question);
      stage = `answer_${item.id}_send`;
      await page.getByRole("button", { name: "Send", exact: true }).click();
      stage = `answer_${item.id}_network_response`;
      const response = await answered();
      const observation = { case_id: item.id, http_status: response.status() };
      report.answer_observations.push(observation);
      check(response.status() === 200, "answer_http_failure");
      stage = `answer_${item.id}_observer_read`;
      const observed = await readObservedAskStream(page, item.question, timeout);
      check(observed.ok, observed.code);
      check(observed.http_status === 200 && observed.request_count === 1, "answer_observer_http_mismatch");
      Object.assign(observation, { transport: "actual_browser_fetch_clone", bytes: observed.bytes,
        request_count: observed.request_count });
      stage = `answer_${item.id}_wire_parse`;
      const answer = terminalAnswer(observed.wire);
      Object.assign(observation, {
        answer_status: /^[a-z_]{1,64}$/.test(answer.status) ? answer.status : "unknown",
        publication_status: /^[a-z_]{1,64}$/.test(answer.publication_status) ? answer.publication_status : "unknown",
        reason_code: /^[A-Z_0-9]{1,64}$/.test(answer.reason_code) ? answer.reason_code : null,
      });
      stage = `answer_${item.id}_assertions`;
      check(answer.status === "answered" && answer.publication_status === "verified", "answer_not_verified");
      check(typeof answer.answer === "string" && answer.answer.trim(), "answer_text_missing");
      // Retain only public synthetic fixture answers in a private temporary
      // manifest for the production offline language detector.
      if (item.language === "ro" || item.language === "pt") answers.push({
        case_id: item.id, language: item.language, answer: answer.answer });
      for (const expected of item.expected_answer_contains) check(
        answer.answer.toLocaleLowerCase().includes(expected.toLocaleLowerCase()), "expected_answer_missing");
      const document = uploads.find(upload => upload.filename === item.expected_source);
      const citation = answer.citations?.find(entry => entry.document_id === document.document_id);
      check(citation?.chunk_id && citation.workspace_id === workspaceId, "expected_source_not_cited");
      stage = `answer_${item.id}_canonical_request`;
      const canonicalResponse = await context.request.get(`${base}/api/passage?${new URLSearchParams({
        workspace_id: workspaceId, chunk_id: citation.chunk_id, document_id: document.document_id })}`);
      check(canonicalResponse.status() === 200, "canonical_citation_unavailable");
      stage = `answer_${item.id}_canonical_body`;
      const canonical = await canonicalResponse.json();
      stage = `answer_${item.id}_canonical_assertions`;
      check(canonical.ok && canonical.passage?.chunk_id === citation.chunk_id &&
        canonical.passage.document_id === document.document_id && canonical.passage.content?.trim(), "canonical_identity_mismatch");
      report.outcomes[item.id] = true;
      const gaps = traceGaps(answer.debug_trace, item.question);
      // The strict trace gate is independent of the multilingual answer gate.
      // Honest not_reported values remain a failed acceptance requirement.
      try {
        stage = `answer_${item.id}_debugger_open`;
        const debug = page.locator("details").filter({ has: page.locator("summary", { hasText: "Debug" }) }).last();
        await debug.locator("summary").click();
        stage = `answer_${item.id}_debugger_visible`;
        const panel = debug.getByRole("complementary", { name: "Answer debugger" });
        await panel.waitFor({ state: "visible", timeout });
        const displayed = await panel.textContent();
        for (const label of ["Query", "Rewrite", "Filters", "Retrieval", "Context", "Generation", "Verification", "Total"])
          check(displayed.includes(label), "debugger_stage_missing");
        check(displayed.includes(answer.debug_trace?.query?.sha256 || "__missing__") &&
          displayed.includes(citation.chunk_id), "debugger_wire_mismatch");
        check(!displayed.includes(canonical.passage.content.trim()), "debugger_source_text_visible");
      } catch { gaps.push("debugger_ui"); }
      report.trace_checks.push({ case_id: item.id, missing: gaps, passed: gaps.length === 0,
        observed_trace: traceEvidence(answer.debug_trace) });
    } catch (error) { failure(item.id, error); }
  }
  report.outcomes.m7_answer_cases = suites.multilingual.every(item => report.outcomes[item.id] === true);
  report.outcomes.golden_answer_cases = suites.golden.every(item => report.outcomes[item.id] === true);
  report.outcomes.m8_trace = report.trace_checks.length === 5 && report.trace_checks.every(item => item.passed);

  for (const [gate, items] of Object.entries(suites)) {
    stage = `ui_eval_${gate}`;
    try {
      await page.goto(`${base}/eval`);
      const datasetName = `live-${runId}-${gate}`;
      const examples = items.map(item => ({ question: item.question,
        expected_answer: item.expected_answer_contains.join("; "),
        expected_sources: [uploads.find(upload => upload.filename === item.expected_source).document_id],
        tags: item.tags }));
      const created = observe(page, response => response.request().method() === "POST" &&
        new URL(response.url()).pathname === `/api/backend/v1/workspaces/${workspaceId}/eval/datasets`);
      await page.getByPlaceholder("name", { exact: true }).fill(datasetName);
      await page.locator("textarea").fill(examples.map(example => JSON.stringify(example)).join("\n"));
      await page.getByRole("button", { name: "Create", exact: true }).click();
      const createdResponse = await created();
      check(createdResponse.status() === 201, "ui_dataset_create_failed");
      const dataset = await createdResponse.json();
      const ran = observe(page, response => response.request().method() === "POST" &&
        new URL(response.url()).pathname === `/api/backend/v1/workspaces/${workspaceId}/eval/runs` &&
        response.request().postDataJSON().dataset_id === dataset.id);
      await page.getByRole("button", { name: `Run ${datasetName}`, exact: true }).click();
      const runResponse = await ran();
      check(runResponse.status() === 200, "ui_eval_run_failed");
      const result = await runResponse.json();
      check(result.n_questions === items.length && result.config_snapshot?.accounting === "retrieval_usage_unreported", "ui_eval_not_full_fresh_run");
      const detail = page.getByRole("region", { name: "Run detail" });
      await detail.getByRole("heading", { name: `Run ${result.run_id}`, exact: true }).waitFor({ state: "visible", timeout });
      const displayed = JSON.parse(await detail.locator("pre").first().textContent());
      check(JSON.stringify(displayed) === JSON.stringify(result.results), "ui_metric_display_mismatch");
      evaluations.push({ gate, dataset_id: dataset.id, run_id: result.run_id, dataset });
      report.outcomes[`ui_eval_${gate}`] = true;
    } catch (error) { failure(`ui_eval_${gate}`, error); }
  }

  stage = "production_cli";
  temporary = mkdtempSync(join(tmpdir(), "live-m7-m8-"));
  const manifest = join(temporary, "manifest.json"), output = join(temporary, "result.json");
  writeFileSync(manifest, JSON.stringify({ workspace_id: workspaceId, uploads, answers, evaluations,
    retrieval: { mode: "hybrid", top_k: 10, reranker: true } }), { mode: 0o600 });
  // No shell and no password/token argv. Capture and discard raw adapter logs;
  // consume only the helper's controlled, text-free report. A nonzero exit
  // still leaves independently completed M7/M8 outcomes available.
  try { await execute(process.env.LIVE_EVAL_PYTHON || "python3", [join(root,
    "tests/fixtures/scripts/live_multilingual_eval_cli.py"), "--manifest", manifest, "--output", output],
    { cwd: root, env: process.env, timeout: Math.min(timeout * 5, 1800000), maxBuffer: 1024 * 1024 });
    report.outcomes.production_cli = true;
  } catch { failure("production_cli", new GateError("production_cli_failed")); }
  report.cli = JSON.parse(readFileSync(output, "utf8"));
  if (report.cli.failure || !Array.isArray(report.cli.failures) || report.cli.failures.length) {
    failure("production_cli", new GateError("production_cli_report_failed"));
  }
  report.outcomes.m7_stored_languages = report.cli.storage?.length === 3 && report.cli.storage.every(item => item.passed);
  report.outcomes.m7_answer_languages = report.cli.answers?.length === 3 && report.cli.answers.every(item => item.passed);
  for (const gate of ["golden", "multilingual"]) report.outcomes[`ui_cli_${gate}`] =
    report.outcomes[`ui_eval_${gate}`] === true && report.cli.evaluations?.some(item =>
      item.gate === gate && item.metrics_equal && item.retrieval_passed) === true;
} catch (error) { failure("infrastructure", error); }
finally {
  if (context) for (const upload of uploads) {
    try {
      const response = await context.request.delete(endpoint(`/documents/${upload.document_id}`));
      check(response.status() === 204, "fixture_delete_failed");
    } catch (error) { stage = "owned_upload_cleanup"; failure("cleanup", error); }
  }
  if (browser) try { await browser.close(); }
  catch (error) { stage = "browser_cleanup"; failure("cleanup", error); }
  if (temporary) rmSync(temporary, { recursive: true, force: true });
}
const required = ["uploads", "m7_answer_cases", "golden_answer_cases", "m7_stored_languages", "m7_answer_languages", "m8_trace", "ui_cli_golden", "ui_cli_multilingual", "production_cli"];
report.passed = required.every(key => report.outcomes[key] === true) && report.failures.length === 0;
if (reportPath) writeFileSync(reportPath, JSON.stringify(report, null, 2), { mode: 0o600 });
console.log(report.passed ? "LIVE_MULTILINGUAL_EVAL_OK" : "LIVE_MULTILINGUAL_EVAL_FAIL",
  JSON.stringify({ outcomes: report.outcomes, failures: report.failures,
    trace_missing: report.trace_checks.map(item => ({ case_id: item.case_id, missing: item.missing })) }));
process.exitCode = report.passed ? 0 : 1;
