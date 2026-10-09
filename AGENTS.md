# AGENTS — Operating Instructions & Team Protocols

> **CONTROLLING PLAN DIRECTIVE (ADOPTED 2026-10-07 / 2026-10-08):**
> All agents working on this codebase must strictly follow the **fork-first operative plan**:
> - [`../rag-fork-solution/PLAN.md`](../rag-fork-solution/PLAN.md)
> - [`../rag-fork-solution/TASKS.md`](../rag-fork-solution/TASKS.md)
> - [`../rag-fork-solution/DECISIONS.md`](../rag-fork-solution/DECISIONS.md)
> - [`../rag-fork-solution/TESTING.md`](../rag-fork-solution/TESTING.md)
> - [`OPERATIVE_PLAN.md`](./OPERATIVE_PLAN.md)

---

## 1. The Team Roles

| Agent | Name / Pane | Role | Primary Responsibilities |
|---|---|---|---|
| **Boss / Coordinator** | `boss` (`w4:p1` / `w4:pV`) | Overall technical authority & dispatcher | Owns scope, dispatches tasks, accepts pull/handoff requests, marks tasks DONE. |
| **Coder 1** | `code1` (`w4:pF` / `w4:p2`) | Backend Engineer | Ingestion, retrieval, adapters, stores, core product API. |
| **Coder 2** | `code2` (`w4:pD` / `w4:p3`) | Frontend & Full-Stack Engineer | Next.js 16 UI screens, search/chat views, backend route extensions. |
| **Reviewer 1** | `review1` (`w4:p6`) | Independent Backend Reviewer | Architecture verification, ACL compliance, contract adherence. |
| **Reviewer 2** | `review2` (`w4:pJ` / `w4:p7`) | Independent Frontend & Cross-Reviewer | UX/UI verification, integration sanity, cross-checks. |
| **Tester** | `test1` (`w4:pE` / `w4:p5`) | QA & Acceptance Probes | Unit/contract/integration test suites, regression gates, acceptance tests #1–#5. |

---

## 2. Mandatory Rules of Engagement

1. **Fork-First Golden Rule**:
   Before writing any subsystem, inspect [`../rag-fork-solution/REUSE_MAP.md`](../rag-fork-solution/REUSE_MAP.md). Reuse verified permissive open-source libraries (docling, LlamaIndex, Qdrant, bge-m3, LiteLLM, RAGAS). Build custom code ONLY for core product glue (ACL, orchestration, citations, product UI/API).
2. **Interface Boundaries**:
   Third-party code lives strictly behind adapter boundaries (`packages/*/interfaces.py`). Vendor SDKs must NEVER leak into core business logic.
3. **ACL Before Ranking (R-004)**:
   Authorization scope is enforced as a hard filter before ranking. No chunk may reach an LLM outside the caller's allowed workspaces.
4. **Citation Authenticity**:
   Citations must resolve to real stored chunks with valid provenance (document, page, section, span) in the caller's workspace. Fabricated citations are rejected.
5. **Model Origin Guard (PD-005 v3)**:
   Non-approved foreign models are strictly barred by `scripts/model_origin_guard.sh`. Default reranker is `cross-encoder/ms-marco-MiniLM-L6-v2`.
6. **Task Completion Authority**:
   Only `boss` marks cards DONE in [`TASKS.md`](../rag-fork-solution/TASKS.md) and [`tasks/QUEUE.md`](../knowledge-os-reuse-first-starter/tasks/QUEUE.md).

---

## 3. The Execution Loop

```text
boss picks TASK from rag-fork-solution/TASKS.md (READY)
   ↓ prompt code1/code2 with the task card
code1/code2 implements within Allowed Files + writes HANDOFF note
   ↓ boss routes diff to review1 or review2
reviewer checks diff: APPROVE / REQUEST-CHANGES
   ↓ boss routes to test1
test1 runs unit + integration + acceptance probes in rag-fork-solution/TESTING.md
   ↓ only after green tests & clean probes
boss commits changes and marks task DONE
```

---

## 4. Handoff Note Format (Required)

```text
TASK: <TASK-ID>
CHANGE: <files touched, one line each>
WHY: <decision + which DECISIONS.md row it satisfies>
REUSED: <GitHub project + license + how it was integrated>
BUILT: <custom code and why reuse did not fit>
VERIFY: <exact commands + expected output>
RISKS: <license / security / perf>
```
