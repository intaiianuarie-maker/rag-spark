# V1 Q5 review — REQUEST CHANGES

Reviewed commit `f49303ee9813ddc40177e445a67d1499abae4a7a` on 2026-10-09. Nothing merged. Q5 and v1 release acceptance remain open.

## Findings

1. **P1 — Token overlap accepts contradictory claims and labels them HIGH confidence.** `packages/answer/verification.py:95-102` discards token order and measures only the fraction of claim tokens occurring somewhere in the source. It cannot distinguish support from contradiction, swapped roles, or changed critical values. The default production grounder feeds directly into `packages/answer/engine.py:215-225`, so the engine returns these unsupported claims as ANSWERED/HIGH:

   | Source | Generated claim | Score | Result |
   |---|---|---|---|
   | The launch is not approved. | The launch is approved. | 1.0 | ANSWERED/HIGH |
   | Alice paid Bob. | Bob paid Alice. | 1.0 | ANSWERED/HIGH |
   | The approved budget for the project is 100 dollars. | The approved budget for the project is 900 dollars. | 0.875 | ANSWERED/HIGH |

   These were reproduced through the actual AnswerEngine with the default grounder and one real in-scope RetrievedItem using the existing fake orchestrator/LLM fixtures. The output answer contains the false claim. Raising the overlap threshold cannot fix the first two cases. Token overlap may be a prefilter, but must not establish substantiation or justify HIGH confidence. Add tests covering negation, entity-role reversal, quantities, dates, and high-overlap contradictions. Implement a support-verification strategy that handles these cases and abstains when support cannot be established; a conservative extractive mode can be an explicitly limited interim strategy, not evidence that semantic Q5 is complete. Withdraw the semantic-grounding/shippable claims until validated.

2. **P2 — Default lexical gating rejects valid cross-language answers.** The same scorer compares surface tokens only. Source `The capital of France is Paris.` and correct Romanian claim `Capitala Franței este Paris.` score 0.25; the default engine returns INSUFFICIENT_EVIDENCE/LOW and drops the citation. Factory wiring enables this gate for every production answer. This conflicts with the EN/RO/PT cross-language requirement. The existing multilingual acceptance probe uses Romanian source and Romanian output and does not exercise translation. Add EN→RO and EN→PT source/answer cases (and a negative mistranslation case); use multilingual support verification or explicitly constrain and disclose the interim mode without claiming the multilingual release gate passes.

## Verification

- `/root/ko-venv/bin/python -m pytest -q tests/unit/test_answer_engine.py tests/integration/test_acceptance_probes.py tests/integration/test_ask_route.py`: **33 passed, 4 skipped in 1.28s**. All four Ask route cases skipped because Postgres/Redis were unreachable. The handoff's claim of 37 passes is not reproduced and must be corrected; these results do not show a live end-to-end release pass.
- `/root/ko-venv/bin/ruff check packages/answer tests/unit/test_answer_engine.py tests/integration/test_acceptance_probes.py`: passed.
- `/root/ko-venv/bin/mypy packages/answer`: passed, 7 source files.
- Standalone reproduction script: `/tmp/v1_q5_review_repro.py` (uses actual engine plus repository test fakes; four results shown above).

The raw-prose suppression and parse/claimless abstention changes address their scoped failure paths. They do not resolve claim-to-source entailment. No Q cards were marked DONE, no release approval issued, and no merge performed.

## Required next handoff

Address the findings within v1 answer/test ownership; return the exact commit/diff, contradiction and multilingual regression results, accurate pass/skip counts, and explicit remaining limits for re-review. Do not merge or modify v2 graph work. Q9 remains a separate live benchmark/release gate.
