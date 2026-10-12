# V1 Q5 coordinator re-review — REQUEST CHANGES

The candidate correction for R7 narrows comma handling and restores the two
reported positive cases. I independently ran the answer-engine suite and Ruff
on the current stable source/test snapshot:

- `pytest -q tests/unit/test_answer_engine.py` — **57 passed**.
- `/root/ko-venv/bin/ruff check packages/answer/verification.py tests/unit/test_answer_engine.py`
  — **All checks passed**.
- `/root/ko-venv/bin/mypy packages/answer` — **Success, 7 source files**.

## P2 — generic reporting-word search still rejects unrelated context

The comma rule checks whether *any* `_REPORTING_VERB` occurs anywhere before
the claim. This remains too broad for ordinary factual assertions:

- `In the note, the launch is approved.` scores **0.0** because `note` is in
  `_REPORTING_VERB`, although here it is a noun in a prepositional phrase.
- `After Bob responded, the launch is approved.` scores **0.0** because
  `responded` occurs in an unrelated temporal clause before the claim.

These are not attribution of the claim to another speaker. They should ground
the verbatim claim, like the already-fixed `On Friday, ...` and `In the report,
...` controls. Narrow detection to actual attribution constructions (for
example, a recognized opener such as `According to <speaker>,` or a reporting
predicate syntactically connected to the claim), rather than searching for a
reporting word anywhere in the prefix. Add these positive cases at both
grounder and answer-engine level; retain the existing attributed negatives.

Direct verification against the current tree returned:

```text
On Friday, the launch is approved.                 1.0
In the report, the launch is approved.             1.0
In the note, the launch is approved.               0.0
After Bob responded, the launch is approved.       0.0
According to Alice, the launch is approved.        0.0
Alice said the launch is approved.                 0.0
The launch is approved, said Alice.                0.0
```

R7's coordinator review remains open pending a fix and re-review. The agent's
draft was preserved separately as unverified; no agent-authored verdict is
accepted. This does not accept V1 product completion or waive Q8/Q9 gates.
