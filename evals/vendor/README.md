# Eval harness vendor registry — attribution + regeneration

**T-EVAL-007.** This directory holds third-party evaluation code vendored so
our results are *leaderboard-comparable*: our row can sit next to a published
row because the numbers come from the official metric code, not a local
re-implementation.

## Standing rules

1. **License check first, internal-eval only.** Every entry in
   `evals/vendor/__init__.py` records the upstream URL and license. No result
   derived from vendored code is published externally.
2. **Nothing is reimplemented-and-called-official.** Where the official
   script cannot be vendored yet, `placeholders.py` carries the *exact output
   contract* and raises `OfficialMetricNotVendored` when called; the
   leaderboard renders those columns as `pending`, never 0.0.
3. **Regenerate verbatim.** Each vendored file is a byte-faithful
   transcription (the corpus tickets cannot download). When network is
   available, regenerate with the `fetch` command in `__init__.py` and diff
   against the transcription; any drift is a finding.

## Register

| File | Status | Upstream | License | Metrics |
|---|---|---|---|---|
| `hotpot_evaluate_v1.py` | vendored | https://github.com/hotpotqa/hotpot/blob/master/hotpot_evaluate_v1.py | MIT | answer EM/F1, supporting-fact EM/P/R/F1, joint EM/P/R/F1 |
| `squad_evaluate_v2.py` | vendored | https://github.com/rajpurkar/SQuAD-explorer/blob/master/dataset/evaluate-v2.0.py | MIT | SQuAD exact match / F1 (XQuAD) |
| `placeholders.py` | placeholder | — | — | 2Wiki/MuSiQue EM/F1 (HotpotQA-shaped), BFCL AST-accuracy (AST-equality) |

## Landed-later surface

`placeholders.py` documents, per pending metric:

* the exact upstream script that must be vendored,
* the exact input/output contract of that script,
* the registry entry to add in `__init__.py`,
* the pull request rule: delete the placeholder only together with the vendored
  file it replaces.

Regenerating:

```bash
curl -sSL -o evals/vendor/hotpot_evaluate_v1.py \
  https://raw.githubusercontent.com/hotpotqa/hotpot/master/hotpot_evaluate_v1.py
```
