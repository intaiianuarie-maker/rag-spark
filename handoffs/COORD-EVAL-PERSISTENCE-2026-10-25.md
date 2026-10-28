# M8-001a / M8-001b — coordinator implementation, review pending

The previous API discarded datasets and runs after responding. Added PostgreSQL
dataset/run storage with workspace checks and migration 0014; list/get/create
dataset and run routes; original `/v1/evaluations/run` + get compatibility API;
immutable golden-input/config snapshots and canonical example SHA256; measured
retrieval latency and imported trace cost/token/latency aggregation. Unreported
retrieval usage is explicitly incomplete. Retrieved contexts/answers are used
transiently and not saved. Added saved-run comparisons validating same dataset,
snapshot/sample/scorer/accounting and exactly one retrieval setting change.

Files: app/eval/{storage,router,runner,models,compare}.py, app/main.py (legacy
registration), alembic/versions/0014_evaluation_storage.py,
tests/integration/test_eval_persistence.py, docs/EVALUATIONS.md.

Reused existing SQLAlchemy/Alembic, authz require, metrics/runner, Pydantic and
retrieval factory; no new dependencies. No golden fixture edits.

Verification: 15 tests passed (eval metrics/comparison + real PostgreSQL
isolated-schema HTTP integration); ruff clean; mypy 8 eval source files clean.
Integration creates/drops only its random eval_test schema, never migrates
public. It proves session/connection disposal readback, workspace denial and
foreign-ID hiding, stable fixture metrics/golden hashes, imported nonzero
accounting, legacy routes, two reranker fixture runs with hand-computed deltas,
invalid dimension rejection and no persisted retrieved text.

Independent review requested from dev1. NOT accepted/DONE/committed/deployed.
Remaining review concerns: live corpus/provider revisions are not frozen by
the snapshot; live embedding usage unavailable from existing retrieval seam;
comparison-config support currently top_k/mode/reranker, original task also
describes other experiment dimensions. Address those against actual acceptance
before closure. Tests may skip loudly when PostgreSQL is unavailable in CI;
local integration was executed and passed.
