# V1 Q5 coordinator re-review — REQUEST CHANGES

The R8 cases now pass in the current implementation. Independent checks on the
stable snapshot:

- `/root/ko-venv/bin/python3 -m pytest tests/unit/test_answer_engine.py -q` —
  **59 passed**.
- Ruff — **All checks passed**.
- mypy `packages/answer` — **7 source files clean**.
- Direct grounder probes: `In the note, ...`, `After Bob responded, ...`, the
  original R7 positives, and the three existing attribution negatives all
  returned their expected scores.

## P2 — reporting verb anywhere after the claim still causes false abstention

Frame 4 checks `_REPORTING_VERB.search(after)` over all remaining text. This
rejects an otherwise plain factual assertion when an unrelated reporting verb
appears later in the same sentence:

```text
The launch is approved, and Bob responded.       -> 0.0
The launch is approved after Bob responded.      -> 0.0
The launch is approved; Bob responded by email.  -> 0.0
```

These are not postposed attribution. Restrict the after-claim case to a
syntactically connected postposed speaker predicate (for example, `, said
Alice`) and add grounder plus answer-engine positive controls for unrelated
following clauses. Keep `The launch is approved, said Alice.` and
`According to Alice, ...` as negative controls.

## P2 — removing ambiguous verbs also permits direct attributed claims

The handoff acknowledges that `Alice reported the launch is approved.` is now
grounded at **1.0**, because `report/reported` was removed from the closed
class. The same applies to `Alice noted the launch is approved.` Restore common
direct attribution forms while keeping noun contexts (`In the report/note,
...`) grounded. Add positive/negative controls at both grounder and answer-engine
levels so the rule distinguishes a subject + reporting predicate from a
prepositional noun phrase.

Please submit the correction in a separate handoff. Do not edit or close any
file under `reviews/`. Q8/Q9 and product acceptance remain open; no merge or
deploy is authorized.
