#!/usr/bin/env node
/**
 * Frontend payload guard vs. the real backend /health payload (M0-004).
 *
 * Cross-contract requested by the M0-003 frontend review: `apps/web/src/lib/health.ts`
 * is the guard the status page trusts, and the backend owns the payload. Node on
 * this host is not built with TypeScript support, so the test compiles `health.ts`
 * with the web app's own `tsc` first and imports the emitted JS here.
 *
 * Usage:
 *   node health_guard_check.mjs <compiled-health.js> <backend-health-payload.json>
 *
 * Prints `HEALTH_GUARD_OK checks=<n>` and exits 0 when every check passes.
 */

import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [, , healthModulePath, payloadPath] = process.argv;
if (!healthModulePath || !payloadPath) {
  console.error("usage: node health_guard_check.mjs <compiled-health.js> <payload.json>");
  process.exit(2);
}

const guard = await import(pathToFileURL(healthModulePath).href);
const { normalizeHealthPayload, overallState, parseHealthApiResponse } = guard;

let checks = 0;
const failures = [];
const check = (name, condition, detail = "") => {
  checks += 1;
  if (!condition) failures.push(`${name}${detail ? ` — ${detail}` : ""}`);
};

const backend = JSON.parse(readFileSync(payloadPath, "utf8"));

// 1. Backend payload -> normalized health (fields the status page renders).
const health = normalizeHealthPayload(backend);
check("backend status passthrough", health.status === backend.status, JSON.stringify(health));
check("backend version passthrough", health.version === backend.version, JSON.stringify(health));
check(
  "backend details keep service/env",
  typeof health.details.service === "string" && typeof health.details.env === "string",
  JSON.stringify(health.details),
);
check("backend payload recognized", health.isEmpty === false, JSON.stringify(health));
check("overallState(healthy) for status=ok", overallState(health) === "healthy", overallState(health));

// 2. Proxy happy path: {ok:true, fetchedAt, health}.
const okWrapper = parseHealthApiResponse({
  ok: true,
  fetchedAt: new Date().toISOString(),
  health,
});
check("proxy ok wrapper parses", okWrapper !== null && okWrapper.ok === true, JSON.stringify(okWrapper));
check(
  "proxy ok wrapper keeps status",
  okWrapper !== null && okWrapper.health.status === backend.status,
  JSON.stringify(okWrapper),
);

// 3. Non-2xx backend with a JSON body (route.ts "backend_unhealthy" branch).
const degraded = {
  status: "degraded",
  service: backend.service ?? "knowledge-os-api",
  version: backend.version ?? "0.1.0",
  env: backend.env ?? "local",
};
const degradedHealth = normalizeHealthPayload(degraded);
check("degraded payload -> overallState degraded", overallState(degradedHealth) === "degraded");
const unhealthy = parseHealthApiResponse({
  ok: false,
  error: "backend_unhealthy",
  message: "Backend responded with HTTP 503.",
  status: 503,
  health: degradedHealth,
});
check(
  "backend_unhealthy wrapper parses",
  unhealthy !== null && unhealthy.ok === false && unhealthy.error === "backend_unhealthy",
  JSON.stringify(unhealthy),
);
check("backend_unhealthy keeps status 503", unhealthy !== null && unhealthy.status === 503);
check(
  "backend_unhealthy keeps degraded health",
  unhealthy !== null && unhealthy.health && unhealthy.health.status === "degraded",
  JSON.stringify(unhealthy),
);

// 4. Non-JSON backend body (route.ts "unexpected_response" branch input).
const emptyHealth = normalizeHealthPayload(null);
check("non-JSON body -> empty health", emptyHealth.isEmpty === true && emptyHealth.status === null);
check("non-JSON body -> overallState unknown", overallState(emptyHealth) === "unknown");

// 5. Every documented proxy error kind is recognized.
for (const error of ["backend_unreachable", "backend_unhealthy", "unexpected_response"]) {
  const parsed = parseHealthApiResponse({ ok: false, error, message: "x" });
  check(`error kind recognized: ${error}`, parsed !== null && parsed.error === error, JSON.stringify(parsed));
}

// 6. Malformed proxy responses are rejected (guard returns null, UI shows unknown).
const malformed = [
  ["ok without fetchedAt", { ok: true }],
  ["fetchedAt not a string", { ok: true, fetchedAt: 42, health: {} }],
  ["unknown error kind", { ok: false, error: "boom", message: "x" }],
  ["missing ok flag", { status: "ok" }],
  ["non-object payload", "not-an-object"],
  ["null payload", null],
];
for (const [name, payload] of malformed) {
  const parsed = parseHealthApiResponse(payload);
  check(`malformed rejected: ${name}`, parsed === null, JSON.stringify(parsed));
}

if (failures.length > 0) {
  console.error(`HEALTH_GUARD_FAILED checks=${checks} failures=${failures.length}`);
  for (const failure of failures) console.error(`  - ${failure}`);
  process.exit(1);
}
console.log(`HEALTH_GUARD_OK checks=${checks}`);
