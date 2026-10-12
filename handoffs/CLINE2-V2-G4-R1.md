# Handoff — V2 G4 integration wiring (protected seam, gated) — submitted for review

> Scope: `feat/v2-graph` (Stream B). Contract: `docs/graph/G4-INTEGRATION-CONTRACT.md`
> §1a/§1b + non-negotiables (R-004, §2a citation guard, flag-off parity).
> Prerequisite `reviews/V2-G5-TEMPLATES-REREVIEW-2.md` is APPROVED.
> Author: dev2 · 2026-10-11. Status: **submitted for review** (not CLOSED).

## TASK

`V2-G4-INTEGRATION-WIRING` (from `/root/code/Super Rag/rag-fork-solution/tasks/`):
wire `GRAPH_MODE` into the production answer orchestrator + `/ask` endpoint.

## CHANGE (exact diff — 3 protected files + 2 test files, allowed-files only)

1. `packages/answer/factory.py` — `create_orchestrator` now does
   `from graph.factory import create_graph_retriever` (lazy, inside the
   function, same style as the other adapter imports) and passes
   `graph=create_graph_retriever()` to `HybridRetrievalOrchestrator(...)`.
   When `GRAPH_MODE=off` the value is `None` and the graph leg no-ops (v1 parity).
2. `packages/answer/engine.py` — `AnswerEngine.answer` accepts
   `mode: RetrievalMode = RetrievalMode.HYBRID` and passes `mode=mode` to
   `self.orchestrator.retrieve(mode=mode, ...)`. Default = today's behavior.
3. `apps/api/app/ask/router.py` — `AskRequest` gains
   `mode: RetrievalMode = RetrievalMode.HYBRID` (imported from
   `retrieval.interfaces`, mirroring `SearchRequest.mode`); `ask(...)` passes
   `mode=body.mode` to `engine.answer(...)`.
4. `tests/unit/test_answer_engine.py` — `_FakeOrchestrator` records
   `last_kwargs`; 4 new G4 tests: default mode is HYBRID, explicit
   `HYBRID_GRAPH` passes through, bare `[Knowledge Graph Context]` passage
   never grounds (abstains `CITATION_INVALID`), hydrated original-chunk text
   with `RetrievalMethod.GRAPH` provenance still grounds normally.
5. `tests/unit/test_orchestrator.py` — 4 new G4 tests: `graph=None` +
   `HYBRID_GRAPH` yields no `graph` trace keys (flag-off parity); foreign-ws
   graph evidence never ranks in the caller's scope (R-004); factory under
   `GRAPH_MODE=off` wires `graph=None` (adapters stubbed); factory under
   `GRAPH_MODE=rule` wires a `GraphRetriever` (tmp `GRAPH_DB_PATH`).

No ingestion, worker, compose, migration, or unrelated-package edits. The §2a
full hydration integration test (`tests/integration/test_graph_citation_hydration.py`)
is NOT added here: it needs the authoritative chunk store + version/ACL fixtures
behind the protected seam, which is follow-up work — instead the unit-level
citation-safety pins above prove the enforceable half of §2a today (the
extractive grounder cannot ground a claim against a bare synthesized passage,
so graph-derived answers abstain rather than cite it).

Note: the tree also contains pre-existing v1-stream diffs I did not author
(`packages/answer/verification.py` attribution frames, bulk of
`test_answer_engine.py` growth). My hunks are: the 3 protected-file diffs
above + `last_kwargs` + the 4 `G4`-marked engine tests + the 4 `G4`-marked
orchestrator tests.

## WHY

G4 contract §1a/§1b: expose the reviewed/approved graph leg (G1–G3) behind the
`GRAPH_MODE` flag with zero behavior change when off. Satisfies the wiring half
of the V2 product-completion seam; UI mode selector stays with frontend stream
(contract §1c, untouched).

## REUSED / BUILT

- REUSED: `graph.factory.create_graph_retriever`, `HybridRetrievalOrchestrator`
  graph leg + ACL-before-ranking, `RetrievalMode`, `ExtractiveGrounder`
  (all in-seam, no new dependency).
- BUILT: thin flag-gated wiring + mode passthrough only (custom glue, per contract).

## VERIFY (exact commands + outputs, repo root `/root/code/Super Rag/knowledge-os`)

- `py_compile factory.py engine.py router.py test_answer_engine.py` → OK.
- `pytest tests/unit/test_graph_factory.py tests/unit/test_orchestrator.py tests/unit/test_answer_engine.py -q` → **104 passed** (was 96 before; +8 new G4 tests).
- `ruff check packages/answer/ apps/api/app/ask/` → **All checks passed**.
- `mypy packages/answer apps/api/app/ask` → **no issues in 9 source files**.
- Flag evidence: `GRAPH_MODE=off` → `answer.mode default = hybrid`,
  `create_graph_retriever() = None`; `GRAPH_MODE=rule` → `GraphRetriever`
  (True); `AskRequest` default `hybrid`, explicit `hybrid_graph` accepted.
- Flag-off parity: all pre-existing answer/orchestrator tests pass unmodified
  (default `HYBRID` + `graph=None` = v1 path); `test_g4_graph_off_means_no_graph_leg`
  pins no `graph` trace keys when off.
- R-004: `test_g4_graph_on_leg_runs_before_ranking_with_acl_scope` pins
  foreign-workspace graph evidence never ranks; orchestrator re-filters the
  graph leg defensively before fusion (unchanged code path).
- §2a: `test_engine_never_grounds_bare_synthesized_graph_context` pins abstain
  on bare synthesized content; positive pin proves hydrated text grounds.

## RISKS

- License/security/perf: none new — stdlib sqlite graph store, in-process,
  no socket/weights/service; unknown `GRAPH_MODE` still raises `ValueError`.
- Known limitation (contract §3, disclosed): fused-path RRF dilution of graph
  items is NOT fixed here — out of scope by contract, candidate follow-up.
- UI has no mode selector yet (§1c, frontend stream); `/ask` defaults to
  `hybrid` so existing clients are unaffected.
