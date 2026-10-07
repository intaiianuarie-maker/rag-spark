# Test Harness — TASK-M0-004

Integration test harness + adapter contracts for the Knowledge OS skeleton.
Scope per `tasks/M0/TASK-M0-004.md`; strategy per
`docs/engineering/testing-strategy.md`; role rules per `.agents/test1-mimo.md`.

**Golden rules encoded here**

1. **Skip loudly, never silent-pass** — every service-dependent check refuses to
   run with a message naming the service, the URL and the command that starts it.
2. **Tests our seams, not upstream libraries** — contract tests target the
   interfaces in `packages/*/interfaces.py`; fakes stand in for adapters that
   do not exist yet (M1-M3).
3. **No model weights, no API keys, no vendor SDKs** — enforced by
   `tests/unit/test_harness_hygiene.py` and by the CI vendor guard.
4. **Security placeholders stay `xfail(strict=False)`** until the owning task
   lands, so M1/M2/M3 turn them green without rewriting the harness.

---

## How to run

```bash
# from the repo root (knowledge-os/)
pytest -q -m "not e2e"     # default local run: unit + contract + integration,
                           # no compose, no node, no network beyond localhost stubs
pytest -q                  # same (e2e tests skip unless RUN_WEB_E2E=1)
make test-unit             # pytest -q -m unit
make test-integration      # pytest -q -m integration  (live checks skip if compose is down)

docker compose up -d       # enables the live checks: /health, /ready, alembic, web proxy
RUN_WEB_E2E=1 pytest -m e2e -q   # `next build` + the 5-case proxy branch matrix
```

### This dev host (see `handoffs/M0-003-*-test1.md`)

```bash
export TMPDIR=/root/.tmp          # host /tmp is a full tmpfs (3.9G, 100%)
# python deps come from requirements-dev.txt:
/root/.venvs/m004/bin/python -m pytest -q        # venv used by test1
# system python3 has no fastapi/httpx — plain `pytest` on PATH will error
```

`conftest.py` also sets `sys.dont_write_bytecode = True`: writing `.pyc` into the
bind-mounted `apps/api` makes the dev container's `uvicorn --reload` restart and
briefly 502 the web proxy.

---

## Markers

| Marker | Meaning | Registered by |
|---|---|---|
| `unit` | runs with no external services (fakes, in-process app) | `pyproject.toml` |
| `integration` | may use compose services; skips with a reason when they are down | `pyproject.toml` |
| `contract` | adapter contract check (reusable checker from `packages/*/contract_tests.py`) | `tests/conftest.py` |
| `e2e` | opt-in (`RUN_WEB_E2E=1`), needs node/npm + compose API | `tests/conftest.py` |

Extra markers are registered in `conftest.py` because `pyproject.toml` is
outside TASK-M0-004's Allowed Files and `--strict-markers` rejects unknown ones.

## Environment variables

| Variable | Default | Used for |
|---|---|---|
| `API_BASE_URL` | `http://127.0.0.1:8000` | live API checks |
| `WEB_BASE_URL` | `http://127.0.0.1:3001` | live web checks (compose publishes **3001** here; `:3000` is occupied) |
| `API_PORT` | `8000` | proxy matrix case A |
| `QDRANT_URL` | `http://127.0.0.1:6333` | live VectorStore contract (M3) |
| `DATABASE_URL` | `postgresql+psycopg://knowledge:knowledge@localhost:5432/knowledge` | alembic + live LexicalStore contract (M3) |
| `RUN_WEB_E2E` | unset | `1` enables `next build` + proxy matrix |
| `TMPDIR` | `/root/.tmp` on this host | temp files (host `/tmp` is full) |
| `MIGRATIONS_ON_BOOT`, `LOG_JSON`, `APP_ENV` | `false`, `false`, `test` | set by `conftest.py` before app import |

---

## Fixture inventory

```text
tests/
├── conftest.py                     # markers, skip-not-pass fixtures, A/B workspace stub, subprocess env
├── fixtures/
│   ├── harness_fakes.py            # fakes for every interface + reference RRF + reference orchestrator
│   ├── service_probe.py            # tcp_open / require_service (skip with an actionable message)
│   ├── pd001/                      # the six PD-001 parser fixtures (see fixtures/pd001/README.md)
│   │   ├── expected_elements.json  # element/page/section/language expectations per fixture
│   │   ├── _generate.py            # stdlib-only authoring script (regenerate: python3 _generate.py)
│   │   ├── pdf_text_multipage.pdf  pdf_table.pdf  docx_headings_table.docx
│   │   ├── html_article_nav.html   markdown_structure.md  csv_multilingual.csv
│   └── scripts/                    # committed ad-hoc probes (landed from /root/.tmp)
│       ├── m004_probe_checks.py    # 11-case readiness probe matrix (unavailable/timeout/malformed/positive)
│       ├── m004_ready_degraded.py  # /ready degraded contract with all deps dead
│       ├── m004_proxy_branches.sh  # 5-case /api/health proxy matrix (isolated next start)
│       ├── m004_stubs.py           # stub backends for the proxy matrix (503 JSON / non-JSON / 5s slow)
│       └── health_guard_check.mjs  # frontend payload guard vs the real backend payload
├── unit/                           # RRF determinism, ACL-before-rank, harness hygiene, landed probes*
├── contracts/                      # one file per adapter interface + health cross-contract
└── integration/                    # live endpoints, alembic idempotency, security xfails, web e2e
```

\* the landed probe scripts are marked `integration`: they open local sockets,
but they need no compose services.

### `packages/*/contract_tests.py`

Reusable checkers next to the interfaces (Allowed Files). Plain async functions
with explicit `AssertionError`s — no pytest dependency — so both this harness
and future adapter tests call the same code:

| Module | Checkers |
|---|---|
| `retrieval.contract_tests` | `check_vector_store_contract`, `check_lexical_store_contract`, `check_embedding_provider_contract`, `check_reranker_contract`, `check_rrf_determinism`, `check_retrieval_results_contract` |
| `ingestion.contract_tests` | `check_document_parser_contract` (+ `ParserFixture`/`ExpectedElement`) |
| `models.contract_tests` | `check_llm_provider_contract` |
| `connectors.contract_tests` | `check_connector_contract` |

---

## Turning a check green later (conventions for M2/M3/M6/M7)

Real-adapter tests **skip loudly** today and pick up the same checker when the
adapter lands. The module must expose a zero-argument factory:

| Interface | Expected module | Expected factory |
|---|---|---|
| `VectorStore` (Qdrant) | `retrieval.adapters.qdrant` | `create_vector_store() -> VectorStore` |
| `LexicalStore` (Postgres FTS) | `retrieval.adapters.postgres_fts` | `create_lexical_store() -> LexicalStore` |
| `DocumentParser` (PD-001) | `ingestion.adapters.<id>` from `expected_elements.json` | `PARSER: DocumentParser` **or** `create_parser() -> DocumentParser` |

Missing module → skip with the module name; present module with the wrong entry
point → **fail** (so naming is a decision, not an accident).

Embedding/reranker/LLM real-adapter runs are deliberately *not* wired here: they
need weights or keys. Those tasks run the same checkers in their own task.

## Contract decisions locked by this harness (for the reviewer)

1. **Fail closed on empty scope** — a `SearchFilter`/`ACLScope` with no
   workspaces returns nothing (`R-004`).
2. **Filter before ranking** — the workspace scope must travel *with* the
   retriever call; rank-then-filter loses every in-scope row when foreign rows
   outrank them (`tests/unit/test_acl_before_rank.py` proves it).
3. **Invalid input raises** — unsupported mime, wrong vector dimension, empty
   bytes, unknown connector id must raise, never return an empty success
   (exception *type* is the adapter's choice).
4. **Provenance survives every hop** — `document_id`, `document_version_id`,
   `chunk_id`, `page`, `section` must be present on retrieved items.
5. **RRF is deterministic and order-independent** — `check_rrf_determinism(fuse)`
   takes any implementation; M3's fusion runs it unchanged.
6. **Manifest is the citation contract** — `expected_elements.json` pins
   page/section/language per fixture; changing an expectation to make a red
   parser pass is a reviewable change, not a silent fix.
7. **Lifecycle is the caller's** — `check_vector_store_contract` never closes
   the store it is handed (construct → check → close in `finally`); the lexical
   caller uses `getattr(store, "close", None)` because the `LexicalStore` ABC
   has no `close()`.

### v1 assumption — `ACLScope.include_public` (code1 review Minor 3)

`SearchFilter` carries `workspace_ids` only, so `include_public` is **not**
propagated to stores. v1 has no public documents: a public-only scope must fail
closed (zero results) instead of ranking everything. M3 either threads
`include_public` through `SearchFilter` and honours it in the adapters, or drops
the field from `ACLScope` — recorded in
`packages/retrieval/contract_tests.py` (module docstring) and
`tests/unit/test_acl_before_rank.py`. A dedicated public-scope test is deferred
to M3, when the field decision exists to test.

## Security placeholders (`xfail(strict=False)`)

| Test | Turns green in | Covers |
|---|---|---|
| `test_workspace_b_cannot_search_workspace_a_secret` | M1-001c + M3-001b | Critical Security E2E (workspace A/B) |
| `test_deleted_document_is_tombstoned_and_never_retrieved` | M2-001d | deleted document tombstone |
| `test_duplicate_sync_does_not_duplicate_documents` | M2-001d / M6-001b | duplicate sync |

Remove the marker in the implementing task; keep `strict=False` until then.
