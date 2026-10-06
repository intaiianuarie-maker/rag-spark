# Test Fixtures

Shared inputs for the harness. Nothing in this directory is product code — it
all lives under `tests/` and exists to make contract checks reusable.

| Path | What it is |
|---|---|
| `harness_fakes.py` | In-memory fakes for every adapter interface (`FakeVectorStore`, `FakeLexicalStore`, `FakeEmbeddingProvider`, `FakeReranker`, `FakeDocumentParser`, `FakeConnector`, `FakeLLMProvider`) plus two labelled **reference** implementations: `reference_rrf` (pinned RRF numbers) and `ReferenceHybridOrchestrator` (ACL-before-rank ordering). M3-001b must satisfy the same assertions; these are not candidates for promotion to `packages/`. |
| `service_probe.py` | `tcp_open()` / `require_service()` — the skip-not-pass helper. Every refusal names the service, the URL and the command that starts it. |
| `pd001/` | The six PD-001 parser fixtures + `expected_elements.json` + authoring script. See `pd001/README.md`. |
| `scripts/` | Committed versions of the ad-hoc probes from the M0-003 test stage (see below). |

## `scripts/` — landed ad-hoc probes

Provenance is kept in each header; these were written as throwaway probes during
M0-003 testing and promoted to fixtures by TASK-M0-004.

| Script | Origin | Asserted by |
|---|---|---|
| `m004_probe_checks.py` | `/root/.tmp/m004_probe_checks.py` (11/11 probe matrix, backend test report) | `tests/integration/test_landed_probe_matrix.py` |
| `m004_ready_degraded.py` | `/root/.tmp/m004_ready_degraded.py` (`DEGRADED_CHECKS_OK`) | `tests/integration/test_landed_ready_degraded.py` |
| `m004_proxy_branches.sh` | `/root/.tmp/m004_proxy_branches.sh` (5/5 proxy matrix, frontend test report) | `tests/integration/test_web_e2e.py` (`RUN_WEB_E2E=1`) |
| `m004_stubs.py` | `/root/.tmp/m004_stubs.py` (503-JSON / non-JSON / 5s-slow stubs) | started by `m004_proxy_branches.sh` |
| `health_guard_check.mjs` | new in M0-004 (frontend↔backend payload cross-contract from the M0-003 review) | `tests/contracts/test_health_cross_contract.py` |

Each script is also runnable by hand — see `tests/README.md`.
