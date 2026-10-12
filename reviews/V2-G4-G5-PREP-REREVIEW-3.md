# V2 preparation correction — APPROVE scoped fix

Reviewed 2026-10-11. The remaining runtime-disclosure finding in
V2-G4-G5-PREP-REREVIEW-2 is closed: run_paired_eval no longer accepts
runtime_mode and assigns fake for its fixed fake-adapter implementation.
The new regression rejects caller attempts to relabel it real.

Independent verification:

- Factory/paired-eval selection: **22 passed in 0.74s**.
- Scoped ruff on factory, fixture, driver and both test files: passed.
- Standalone driver: exit 0, both context windows show SYNTHETIC corpus,
  FAKE runtime and grounded-success UNMEASURED. No production gain claimed.

Reviewed SHA256:

- tests/fixtures/graph_paired_eval.py:
  765cd13048521a23d9547eb072cfe3699b761cce84a9ef21353c70be9f54c571
- tests/unit/test_graph_paired_eval.py:
  45a4c657fc1bbc6f3402b435b57d2069515d4de895715f6dc21be7796ffb7963

Approval is limited to reviewed prep corrections. The reported full 132 tests
and graph mypy were not independently repeated for this small final change.
G4 protected wiring, V1 Q9, real G5 evaluation and generated-answer success
remain open. No V2 product-completion verdict, merge or deployment authorization.

Next authorized independent V2 task: prepare G5 reproducibility artifacts in
evals/docs/graph and new eval tests: dataset manifest with source hashes,
source-family dev/holdout partitions and gold chunk/source references; validate
no family overlap or dangling labels; inventory actual available source material
and identify missing annotations. Do not invent labels for missing documents or
call synthetic/fake results real. Provide a reviewed execution contract for a
separate real-adapter paired runner with equal budgets, per-slice metrics and
confidence intervals. No production ingestion/service calls, new dependencies,
protected code edits or holdout tuning. Send scoped handoff and concrete inputs
still needed. V3/V4 remain deferred.
