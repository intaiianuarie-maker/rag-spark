# V1 Q5 coordinator re-review — REQUEST CHANGES

The latest source closes indirect attribution and fixes the reported Ruff
RUF002 lint issue. I independently ran the answer-engine suite and direct
grounder probes against the current tree.

## P2 — comma-fronted check still rejects ordinary factual context

`_is_attributed_frame()` still treats every comma before a claim as an
attribution frame. Current direct probes return `0.0` for all of these:

- `On Friday, the launch is approved.` — should ground the verbatim claim.
- `In the report, the launch is approved.` — should ground the verbatim claim.
- `According to Alice, the launch is approved.` — should abstain.
- `Alice said the launch is approved.` — should abstain.
- `The launch is approved, said Alice.` — should abstain.

The attribution negatives now behave correctly, but the positional comma rule
still causes false abstention on ordinary context-setting clauses. Restrict the
fronted frame to recognized attribution syntax, and add positive answer-engine
controls for the first two examples while retaining the three attribution
negatives.

## Verification

- `pytest -q tests/unit/test_answer_engine.py` — **55 passed**.
- `/root/ko-venv/bin/ruff check packages/answer/verification.py tests/unit/test_answer_engine.py`
  — **All checks passed**.
- Direct `ExtractiveGrounder.score()` probes reproduce the false abstentions
   above.

This re-review does not accept V1 Q5 yet, and does not accept V1 product
completion or waive Q8/Q9 quality and release gates.
