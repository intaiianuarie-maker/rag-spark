# Coordinator checkpoint review — 2026-10-11

This is a source-level follow-up to `V1-Q5-REREVIEW-4.md` and
`V2-G5-MANIFEST-REVIEW-1.md`. No merge or release approval is granted.

## V1 Q5 — still REQUEST CHANGES

The specific examples in REREVIEW-4 now behave correctly:

- `Alice challenged the assertion: the launch is approved.` scores `0.0` for
  `The launch is approved.`
- `“The launch is approved,” said Alice.` scores `0.0`.
- `locate("The launch is approved.", "Straße. The launch is approved.")`
  returns `(8, 30)`, an original-text span.

However, attribution is still incomplete. Current source probes return `1.0`
for both `Alice said the launch is approved.` and `According to Alice, the
launch is approved.` The current guard recognizes quoted spans and colon
introductions, so ordinary indirect attribution is still promoted to a factual
claim. Keep Q5 open until attributed speech is represented/validated with
context-preserving semantics (including indirect attribution) and the answer
engine is tested to abstain. Add a permanent Unicode-offset regression for the
`Straße` case; current answer tests do not contain one.

## V2 G5 manifest — REQUEST CHANGES remains

The validator still returns `ok=True` for `dev={real}`, `holdout={}` when the
known family set is `{real}`; it never checks assigned IDs against that set.
The builder still creates each inventory's `rel_path` relative to its own root,
then merges family dictionaries from FinQA and TAT-DQA. Same relative paths can
therefore collide in `DatasetManifest.family_of` and cannot be resolved from the
single manifest root. Qualify each source path under the dataset root (or retain
separate roots), reject collisions, and test that hashes resolve to the correct
source files. Also reject unknown family IDs and duplicate partition identities,
and report empty holdout as not evaluation-ready.

## Verification

`PYTHONPATH=evals pytest -q tests/unit/test_answer_engine.py
tests/unit/test_graph_dataset_manifest.py` → **57 passed**.

The original review repros were run directly against current code. No fixes were
made by the coordinator; implementation agents remain quota-blocked. Q9/G4/G5
release gates remain in effect.
