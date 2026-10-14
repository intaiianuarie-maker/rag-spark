# T-ARCH-01: Architectural Audit of Local-Model Touchpoints

**Date**: 2026-10-14  
**Author**: dev2 (full-stack coding agent)  
**Status**: COMPLETE (READ-ONLY AUDIT)  
**Context**: User-ordered architectural pivot to **OpenRouter models ONLY**. The local infinity container (`knowledge-os-infinity-rerank-1`), its 14.3 GB image, and its docker-compose service block have been removed by museR. This audit inventories every remaining local-model touchpoint across the repository.

---

## 1. Executive Summary & Sweep Verification

Per ticket requirements, 10 targeted sweeps were executed across all source files, configurations, scripts, tests, evaluations, and documentation. Zero local-model touchpoints remain unlisted.

| Sweep Pattern | Occurrences | Matched Files | Scope & Nature of Matches |
| :--- | :---: | :---: | :--- |
| **`infinity`** | 380 | 69 | Service name, client adapters, readiness probes, compose comments, test fixtures |
| **`bge`** | 193 | 39 | Model pins (`BAAI/bge-m3`), audit scripts, dimension assertions, documentation |
| **`small`** | 87 | 49 | OpenRouter fallback embedding model (`text-embedding-3-small`), small model references |
| **`llama`** | 112 | 30 | LlamaIndex chunking adapter, Ollama/llama.cpp local provider seams in router |
| **`ollama`** | 13 | 9 | Router interface strings, test hygiene assertions, contract tests (verified 0 active daemons) |
| **`sentence-transformer`** | 2 | 2 | 0 imports; 2 hygiene / test checks asserting prohibited library is NOT imported |
| **`cross-encoder`** | 19 | 9 | `cross-encoder/ms-marco-MiniLM-L6-v2` reranker pin, origin guard script, tests |
| **`rerank`** | 363 | 58 | Orchestrator reranking stage, adapter, error classes (`RERANK_UNAVAILABLE`), contract tests |
| **`1024`** | 214 | 54 | 1024-dimension vector pins (bge-m3 dense vectors), buffer sizes in tests/scripts |
| **`7997`** | 48 | 16 | Infinity HTTP port (`127.0.0.1:7997`, `http://infinity-rerank:7997`) |

**Summary Totals**:
- **1,431 match occurrences** across **1,169 unique lines** in **172 files**.
- **Sentence-transformers imports**: **0** (verified via `grep -rn 'import sentence_transformers'` -> 0 matches).
- **Files modified**: **0 existing files** (read-only audit; zero code modifications).

---

## 2. Cutover Ownership & Ticket Mapping

The migration from local models to OpenRouter-only is tracked under tickets `T-ARCH-01` through `T-ARCH-05`:

1. **`T-ARCH-01`** (This ticket, dev2): Complete inventory and audit of all local model touchpoints (read-only).
2. **`T-ARCH-02`** (Abstraction): Provider interface generalization; decoupling provider-specific assumptions from `retrieval/interfaces.py`, `retrieval/orchestrator.py`, and `answer/factory.py`.
3. **`T-ARCH-03`** (Rerank Decision): Cutover or drop local cross-encoder reranker; update search orchestrator, error handling, and `search.py`.
4. **`T-ARCH-04`** (Embed Cutover & Production Wiring): Cutover production embeddings from `bge-m3` (1024-d) to `google/gemini-embedding-2` (3072-d); update `.env.example`, `core/config.py`, worker `ingest_pipeline.py`, api `readiness.py`, `qdrant_store.py`, `embedding_identity.py`, and test suites.
5. **`T-ARCH-05`** (R07 Rebuild on Gemini, dev1): Manifest-driven vector reindex and scratch collection rebuild on OpenRouter Gemini embeddings.

---

## 3. Master Touchpoint Inventory Table

### A. Configuration & Infrastructure

| File:Lines | Touchpoint Kind | Code Snippet / Context | Cutover Owner |
| :--- | :--- | :--- | :--- |
| `packages/core/config.py:60-61` | `INFINITY_URL`, `7997`, `bge-m3` | `infinity_url: str = "http://localhost:7997"` (bge-m3 embeddings + rerank) | **T-ARCH-04** |
| `.env.example:10` | `7997`, `infinity` | Comment: `api/worker use http://infinity-rerank:7997 on the compose` | **T-ARCH-04** |
| `.env.example:128-137` | `bge-m3`, `cross-encoder`, `7997`, `1024` | `INFINITY_URL=http://localhost:7997`, `EMBEDDING_MODEL=BAAI/bge-m3`, `EMBEDDING_DIMENSION=1024`, `EMBEDDING_REVISION=BAAI/bge-m3` | **T-ARCH-04** |
| `.env.example:140-141` | `cross-encoder`, `rerank` | `RERANK_MODEL=cross-encoder/ms-marco-MiniLM-L6-v2` | **T-ARCH-03** |
| `docker-compose.yml:21-28` | `7997`, `infinity` | Header comments: `infinity 127.0.0.1:7997 embeddings + rerank model server` | **T-ARCH-04** |
| `docker-compose.yml:85-86` | `1024`, `bge-m3` | Comment: `BAAI/bge-m3 (1024-d) now served on infinity-rerank` | **T-ARCH-04** |
| `docker-compose.yml:103` | `INFINITY_URL`, `7997` | Environment injection: `INFINITY_URL: ${INFINITY_URL:-http://infinity-rerank:7997}` | **T-ARCH-04** |
| `docker-compose.yml:262-266` | `infinity`, `bge-m3`, `rerank` | ARCH pivot comment: infinity-rerank service removed, image deleted | **T-ARCH-04** |

---

### B. Core Embedding Adapters & Vector Storage

| File:Lines | Touchpoint Kind | Code Snippet / Context | Cutover Owner |
| :--- | :--- | :--- | :--- |
| `packages/retrieval/adapters/infinity_embed.py:1-235` | `infinity`, `bge-m3`, `1024`, `7997` | Full `InfinityEmbeddingProvider` class (`DEFAULT_BASE_URL="http://localhost:7997"`, `DEFAULT_MODEL="BAAI/bge-m3"`, `DEFAULT_DIMENSION=1024`). Speaks `POST /embeddings`. | **T-ARCH-04** |
| `packages/retrieval/adapters/qdrant_store.py:8` | `1024`, `bge-m3`, `infinity` | Comment: `"dense": bge-m3 1024-d, Cosine (served by infinity)` | **T-ARCH-04** |
| `packages/retrieval/adapters/qdrant_store.py:110` | `infinity` | `from .infinity_embed import _is_zero_vector` | **T-ARCH-04** |
| `packages/retrieval/adapters/qdrant_store.py:114` | `1024` | `DEFAULT_DENSE_DIM = 1024` (Qdrant collection vector size default) | **T-ARCH-04** |
| `packages/retrieval/embedding_identity.py:18-100` | `1024`, `bge-m3` | Embedding vector identity verification asserting `dimension == 1024` and model signature | **T-ARCH-04** |
| `packages/retrieval/interfaces.py:46-51` | `1024`, `bge-m3` | `EmbeddingProvider` ABC declaring `dimension: int = 1024`, `model_name = "BAAI/bge-m3"` | **T-ARCH-02** |

---

### C. Local Cross-Encoder Reranker & Retrieval Orchestrator

| File:Lines | Touchpoint Kind | Code Snippet / Context | Cutover Owner |
| :--- | :--- | :--- | :--- |
| `packages/retrieval/adapters/infinity_rerank.py:1-135` | `cross-encoder`, `infinity`, `rerank`, `7997` | Full `InfinityReranker` adapter (`DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L6-v2"`, `DEFAULT_BASE_URL = "http://localhost:7997"`, `RERANK_PATH = "/rerank"`). | **T-ARCH-03** |
| `packages/retrieval/interfaces.py:80-92` | `rerank` | `Reranker` ABC definition and `RetrievalMethod.RERANKED` | **T-ARCH-02** / **T-ARCH-03** |
| `packages/retrieval/orchestrator.py:7-628` | `rerank` | `HybridRetrievalOrchestrator` rerank stage: calls `self.reranker.rerank(query, items)`, attaches scores, records `RERANK_UNAVAILABLE` / `RERANK_FAILED` | **T-ARCH-02** / **T-ARCH-03** |
| `packages/answer/factory.py:46-60` | `infinity`, `rerank` | `create_orchestrator`: imports and instantiates `InfinityEmbeddingProvider` and `InfinityReranker` | **T-ARCH-02** / **T-ARCH-04** |
| `apps/api/app/search/search.py:24, 97` | `rerank` | Reranker status handling in API search route (`RERANK_UNAVAILABLE` degraded mode) | **T-ARCH-03** |
| `packages/core/errors.py:138-160` | `rerank` | `RerankError`, `RerankUnavailableError`, `RerankFailedError` definitions | **T-ARCH-03** |

---

### D. API & Worker Runtime Call Sites

| File:Lines | Touchpoint Kind | Code Snippet / Context | Cutover Owner |
| :--- | :--- | :--- | :--- |
| `apps/worker/jobs/ingest_pipeline.py:952-961` | `infinity`, `bge-m3` | `from retrieval.adapters.infinity_embed import InfinityEmbeddingProvider`; `embed_provider = InfinityEmbeddingProvider()` (ingest embedding generation) | **T-ARCH-04** |
| `apps/worker/requirements.txt:28` | `infinity`, `rerank` | Comment: `# Retrieval HTTP adapters (TASK-M3-002/M3-004): infinity /embeddings + /rerank` | **T-ARCH-04** |
| `apps/api/app/adapters/infinity_probe.py:1-44` | `infinity`, `7997`, `rerank` | Readiness probe querying `infinity_url` (`GET /health` or `GET /models`) on port 7997 | **T-ARCH-04** |
| `apps/api/app/adapters/readiness.py:23-25` | `infinity`, `rerank` | Readiness documentation: `infinity: embeddings and reranker` | **T-ARCH-04** |
| `apps/api/app/adapters/readiness.py:78, 94` | `infinity` | Imports `infinity_probe` and includes `"infinity"` in dependency list | **T-ARCH-04** |
| `apps/api/app/adapters/readiness.py:454` | `infinity` | Probe execution: `"infinity": infinity_probe.probe(settings.infinity_url, timeout)` | **T-ARCH-04** |
| `apps/api/requirements.txt:50` | `infinity` | Comment: `# infinity; sqlalchemy for PG FTS already present` | **T-ARCH-04** |

---

### E. Reindex, Rebuild & Audit Tooling (Scripts)

| File:Lines | Touchpoint Kind | Code Snippet / Context | Cutover Owner |
| :--- | :--- | :--- | :--- |
| `scripts/reindex_all_chunks.py:6-420` | `infinity`, `bge-m3`, `1024` | Recovery reindex script: instantiates `InfinityEmbeddingProvider()`, probes 1024 dimensions, embeds batches | **T-ARCH-04** |
| `scripts/rebuild_with_manifest.py:6, 200-206` | `infinity`, `bge-m3`, `1024` | Rebuild script: `from retrieval.adapters.infinity_embed import InfinityEmbeddingProvider`; `provider = InfinityEmbeddingProvider(timeout=...)` | **T-ARCH-05** |
| `scripts/audit_rebuilt_index.py:322-472` | `infinity`, `bge-m3`, `1024` | Rebuild audit script: asserts dense vector `dim == 1024`, `model == "BAAI/bge-m3"`, `provider == "InfinityEmbeddingProvider"` | **T-ARCH-05** |
| `scripts/spotcheck_rebuilt_index.py:69, 85` | `infinity` | Rebuild spotcheck: imports and runs `InfinityEmbeddingProvider()` | **T-ARCH-05** |
| `scripts/model_origin_guard.sh:4-55` | `bge-m3`, `cross-encoder` | CI origin gate: enforces `ALLOWED=("cross-encoder/ms-marco-MiniLM-L6-v2" "BAAI/bge-m3")` and inspects `infinity_rerank.py` | **T-ARCH-04** / **T-ARCH-03** |

---

### F. Model Router & Local LLM Seams

| File:Lines | Touchpoint Kind | Code Snippet / Context | Cutover Owner |
| :--- | :--- | :--- | :--- |
| `packages/models/router_config.py:207-227` | `local`, `llama`, `ollama` | `PROVIDER_LOCAL = "local"`, `LOCAL_ONLY_MODEL_ENV = "LOCAL_ONLY_MODEL"`, `DEFAULT_LOCAL_GATEWAY_URL = "http://127.0.0.1:4000"` | **T-ARCH-02** / **V4-GATED** |
| `packages/models/router_config.py:234-260` | `local` | `LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})`, `is_local_endpoint()` string validation | **T-ARCH-02** / **V4-GATED** |
| `packages/models/interfaces.py:7` | `ollama`, `llama` | Docstring comment: `- local vLLM / Ollama / llama.cpp servers via their OpenAI-compatible endpoints` | **T-ARCH-02** |
| `packages/models/contract_tests.py:5` | `ollama`, `vllm` | Comment: `OpenAI-compatible/vLLM/Ollama adapters` | **T-ARCH-02** |
| `packages/models/__init__.py:3-4` | `ollama`, `vllm` | Comment: `Provider adapters (OpenAI-compatible, vLLM, Ollama, ...)` | **T-ARCH-02** |
| `packages/connectors/interfaces.py:12` | `llama` | Reference to LlamaIndex interface types | **INFORMATIONAL** |
| `packages/ingestion/adapters/llamaindex_chunker.py:1-120` | `llama` | LlamaIndex chunker adapter (library name; no local model execution) | **INFORMATIONAL** |

---

### G. Unit, Contract & Integration Tests Asserting Local Model Behavior

| File:Lines | Touchpoint Kind | Code Snippet / Context | Cutover Owner |
| :--- | :--- | :--- | :--- |
| `tests/contracts/test_infinity_embedding_contract.py:1-500` | `infinity`, `bge-m3`, `1024` | 40 assertions testing `InfinityEmbeddingProvider`, mocking `POST /embeddings`, validating 1024-d vectors | **T-ARCH-04** |
| `tests/contracts/test_infinity_reranker_contract.py:1-196` | `infinity`, `cross-encoder`, `rerank` | 42 assertions testing `InfinityReranker`, mocking `POST /rerank`, validating `cross-encoder/ms-marco-MiniLM-L6-v2` | **T-ARCH-03** |
| `tests/contracts/test_embedding_provider_contract.py:3-4` | `1024`, `bge-m3` | Embedding contract tests checking default dimension = 1024 and model name | **T-ARCH-04** |
| `tests/contracts/test_reranker_contract.py:1-43` | `rerank` | Generic contract tests for `Reranker` ABC | **T-ARCH-03** |
| `tests/integration/test_live_endpoints.py:102-150` | `infinity_probe` | Tests asserting `/ready` reports `status == "ready"` with `"infinity"` in dependencies | **T-ARCH-04** |
| `tests/integration/test_dedup_versions.py:344-1450` | `1024`, `infinity` | 14 test cases testing document dedup with 1024-d vectors and `InfinityEmbeddingProvider` | **T-ARCH-04** |
| `tests/unit/test_embedding_identity.py:44-256` | `1024`, `bge-m3`, `infinity` | 18 tests asserting vector collection identity stamps: `provider="InfinityEmbeddingProvider"`, `model="BAAI/bge-m3"`, `dim=1024` | **T-ARCH-04** |
| `tests/unit/test_embedding_identity_boundary.py:56-409` | `bge-m3` | 4 tests asserting identity rejection on non-bge-m3 vectors | **T-ARCH-04** |
| `tests/unit/test_reindex.py:19-550` | `infinity`, `bge-m3`, `1024` | 11 tests verifying `reindex_all_chunks.py` using `InfinityEmbeddingProvider` | **T-ARCH-04** |
| `tests/unit/test_rebuild_with_manifest.py:5-127` | `infinity`, `bge-m3`, `1024` | 8 tests verifying `rebuild_with_manifest.py` using `InfinityEmbeddingProvider` | **T-ARCH-05** |
| `tests/unit/test_audit_rebuilt_index.py:36-242` | `infinity`, `bge-m3`, `1024` | 11 tests verifying index auditing with 1024-d bge-m3 vectors | **T-ARCH-05** |
| `tests/unit/test_orchestrator.py:5-714` | `infinity`, `rerank` | 39 tests testing orchestrator behavior with `InfinityEmbeddingProvider` and `InfinityReranker` | **T-ARCH-04** / **T-ARCH-03** |
| `tests/unit/test_leg_failure_statuses.py:5-717` | `rerank` | 22 tests verifying `RERANK_FAILED` and `RERANK_UNAVAILABLE` degradation in search results | **T-ARCH-03** |
| `tests/unit/test_harness_hygiene.py:22-26` | `sentence_transformers`, `ollama` | Hygiene tests asserting `sentence_transformers` and `ollama` are not installed / imported | **INFORMATIONAL** |
| `tests/unit/test_router_config.py:1-120` | `local` | Tests verifying local model routing rules and sensitive workspace local-only veto | **T-ARCH-02** |
| `tests/unit/test_router_precedence_lock.py:1-80` | `local` | Tests verifying `policy_veto_local_only` precedence in router | **T-ARCH-02** |
| `tests/unit/test_local_only_no_egress.py:1-60` | `local` | Tests verifying sensitive workspaces fail closed if no local model is available | **T-ARCH-02** |
| `tests/fixtures/scripts/t021_crash_worker.py:49-144` | `infinity`, `1024` | Crash test worker fixture importing `InfinityEmbeddingProvider` and writing 1024-d points | **T-ARCH-04** |
| `tests/fixtures/scripts/m004_ready_degraded.py:39` | `7997`, `INFINITY_URL` | Fixture setting degraded `INFINITY_URL` | **T-ARCH-04** |

---

### H. Evals & Benchmarks

| File:Lines | Touchpoint Kind | Code Snippet / Context | Cutover Owner |
| :--- | :--- | :--- | :--- |
| `evals/embedder_clients.py:10-43, 231-285` | `bge-m3`, `1024`, `7997`, `infinity` | `BgeM3Local` client definition targeting `http://127.0.0.1:7997`, `BAAI/bge-m3`, 1024-d | **EVALS-MAINT** |
| `evals/embedder_separation.py:1-250` | `bge-m3`, `1024` | Benchmark script measuring retrieval separation between bge-m3 and Gemini | **EVALS-MAINT** |
| `evals/graph_paired_real_eval.py:21, 60, 166-215` | `bge-m3`, `infinity`, `7997` | Paired graph eval smoke script: attempts local infinity `bge-m3` before OpenRouter fallback | **EVALS-MAINT** |
| `evals/hotpot_full.py:270` | `bge-m3`, `1024` | Distractor dataset loader comments referencing 1024-d vectors and bge-m3 embeddings | **EVALS-MAINT** |
| `evals/test_embedder_separation.py:1-120` | `bge-m3`, `1024` | Unit tests for embedder separation benchmark | **EVALS-MAINT** |
| `evals/test_hotpot_full.py:271` | `sentence_transformers` | Test asserting hotpot dataset loader does not import `sentence_transformers` | **EVALS-MAINT** |

---

### I. Documentation & Architecture Records

| File:Lines | Touchpoint Kind | Code Snippet / Context | Cutover Owner |
| :--- | :--- | :--- | :--- |
| `README.md:111-113` | `bge-m3`, `ollama` | Architecture table: `bge-m3 / OpenAI embeddings`, `OpenAI-compatible / vLLM / Ollama` | **DOCS-UPDATE** |
| `OPERATIVE_PLAN.md:20, 37` | `bge-m3`, `cross-encoder` | Operative plan: M3 bge-m3, bge-reranker, model origin guard allowlist | **DOCS-UPDATE** |
| `AGENTS.md:45, 53` | `bge-m3`, `cross-encoder` | Agent instructions on approved models and `model_origin_guard.sh` | **DOCS-UPDATE** |
| `docs/graph/G4-INTEGRATION-CONTRACT.md:20` | `infinity`, `bge-m3` | Contract notes on hybrid retrieval with infinity and bge-m3 | **DOCS-UPDATE** |
| `docs/graph/G5-REAL-RUNNER-CONTRACT.md:15` | `infinity`, `bge-m3` | Contract notes on evaluation using infinity container | **DOCS-UPDATE** |

---

## 4. Key Architectural Findings & Cutover Recommendations

1. **Production Pipeline Immediate Impact**:
   - `apps/worker/jobs/ingest_pipeline.py:961` imports and executes `InfinityEmbeddingProvider()`. Document ingestion is currently broken until cut over to an OpenRouter embedding provider (`T-ARCH-04`).
   - `apps/api/app/adapters/readiness.py:454` probes `settings.infinity_url`. `/ready` will fail or report degraded status until cut over or removed (`T-ARCH-04`).
   - `packages/answer/factory.py:56-57` wires `InfinityEmbeddingProvider` and `InfinityReranker` into the API query path (`T-ARCH-04`, `T-ARCH-03`).

2. **Reranker Decoupling**:
   - `packages/retrieval/adapters/infinity_rerank.py` is the only active reranker adapter in production code.
   - When cutting over (`T-ARCH-03`), setting `reranker = None` cleanly disables the reranking stage without breaking the retrieval pipeline, while keeping all search contracts valid.

3. **Vector Dimension Shift (1024 -> 3072)**:
   - `BAAI/bge-m3` produced 1024-d vectors; `google/gemini-embedding-2` produces 3072-d vectors.
   - All Qdrant collection configurations (`DEFAULT_DENSE_DIM = 1024` in `packages/retrieval/adapters/qdrant_store.py`) must be updated to 3072-d (`T-ARCH-04`).
   - Existing Qdrant points indexed at 1024-d must be rebuilt into a fresh collection (`T-ARCH-05`, dev1).

4. **Zero Prohibited Dependencies**:
   - Zero instances of `import sentence_transformers` exist in the entire repository.
   - Zero local Ollama/vLLM daemons are running or required.
   - Clean architecture: all local inference was cleanly decoupled behind HTTP calls, allowing straightforward replacement with OpenRouter endpoints.
