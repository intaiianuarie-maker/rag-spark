# OPERATIVE PLAN & SINGLE SOURCE OF TRUTH (FOR ALL AGENTS)

> **CRITICAL DIRECTIVE FOR ALL AGENTS (boss, code1, code2, review1, review2, test1):**
> As of October 7–8, 2026, the controlling, operative plan for this project is:
> - **[`Super Rag/rag-fork-solution/PLAN.md`](file:///root/code/Super%20Rag/rag-fork-solution/PLAN.md)**
> - **[`Super Rag/rag-fork-solution/TASKS.md`](file:///root/code/Super%20Rag/rag-fork-solution/TASKS.md)**
> - **[`Super Rag/rag-fork-solution/README.md`](file:///root/code/Super%20Rag/rag-fork-solution/README.md)**
> - **[`Super Rag/rag-fork-solution/DECISIONS.md`](file:///root/code/Super%20Rag/rag-fork-solution/DECISIONS.md)**
> - **[`Super Rag/rag-fork-solution/TESTING.md`](file:///root/code/Super%20Rag/rag-fork-solution/TESTING.md)**

---

## 1. Plan Authority & Precedence

1. **Controlling Plan**: `Super Rag/rag-fork-solution/PLAN.md` & `TASKS.md` supersedes all legacy milestones and long-term draft build plans.
2. **v1 Scope (Stop at M8)**:
   - **M0**: Foundation (monorepo + infra + CI)
   - **M1**: Auth + workspaces (custom ACL model + policy)
   - **M2**: Ingestion (Docling parser, LlamaIndex parent-child + semantic chunker)
   - **M3**: Retrieval (Qdrant dense + sparse BM25, PG FTS, bge-m3, bge-reranker, ACL-aware RRF orchestrator)
   - **M4**: RAG answers (LiteLLM, grounded claims + verified citations)
   - **M5**: Ask & Search UI (Next.js 16 screens, Ask chat + Search UI)
   - **M6**: Connectors (upload, web via Crawlee, S3 via SeaweedFS [PD-005], Git)
   - **M7**: Model router + multilingual (per-workspace routing rules, RO/PT/EN cross-lingual)
   - **M8**: Debugger + eval (RAGAS metrics, golden dataset, trace UI)
3. **v2+ Sequencing (Explicit Post-v1 Roadmap)**:
   > *"Stop at M8 for v1. Then: v2 graph (LightRAG/GraphRAG) · v3 agents+research (LangGraph/STORM) · v4 workflows (Langflow)."*

---

## 2. Mandatory Rules for All Agents

1. **Fork-First Golden Rule**: Before writing any subsystem, check `rag-fork-solution/REUSE_MAP.md` for existing permissive open-source solutions. Build custom code ONLY for the core product glue (ACL, orchestration, citations, product UI/API).
2. **Interface Isolation**: Third-party code lives strictly behind adapter boundaries (`packages/*/interfaces.py`). Vendor SDKs must NEVER leak into core business logic.
3. **ACL Before Ranking (R-004)**: Authorization scope is enforced as a hard filter before ranking. No chunk may reach an LLM outside the caller's allowed workspaces.
4. **Citation Authenticity**: Citations must resolve to real stored chunks with valid provenance (document, page, section, span) in the caller's workspace. Fabricated citations are rejected.
5. **No Non-Approved Foreign Models (PD-005 v3)**: Enforced via `scripts/model_origin_guard.sh`. Default reranker is `cross-encoder/ms-marco-MiniLM-L6-v2`.

---

## 3. Directory Mapping

- **Application Monorepo**: `/root/code/Super Rag/knowledge-os/`
- **Operative Plan & Task Definitions**: `/root/code/Super Rag/rag-fork-solution/`
- **Loop Protocol & Queue Tracking**: `/root/code/Super Rag/knowledge-os-reuse-first-starter/`
- **Virtual Environment**: `/root/ko-venv` (Python 3.14.4)
