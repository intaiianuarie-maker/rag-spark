# V1 Q5 coordinator re-review — APPROVED (P1 closed)

Reviewed the current `packages/answer/verification.py` and the follow-up tests in
`tests/unit/test_answer_engine.py` after the 2026-10-11 update.

The previously reported explicit challenge and quoted-speech examples now
abstain (`score=0.0`), and the `Straße` span case returns the correct original
coordinates. New answer-engine tests cover these forms and the citation slice.

## P1 — indirect attribution still becomes HIGH — CLOSED

Prior state: claim `The launch is approved.` returned `1.0` for both:

- source `Alice said the launch is approved.` (indirect speech via reporting verb)
- source `According to Alice, the launch is approved.` (comma-fronted attribution)

The structural guard previously rejected only quoted spans and colon-introduced
claims. These ordinary indirect-report constructions have neither marker, so
the verbatim fragment was treated as the source's own factual assertion.

### Resolution

The context-preserving grounding contract was extended to cover indirect
attribution as **Frame 4** in `_is_attributed_frame()`. The guard now detects
four attribution frames (per the updated docstring contract):

1. **quoted** — a quotation mark abuts the claim (`"X," said Alice.`);
2. **colon** — a colon introduces the claim (`Alice challenged: X`);
3. **comma-fronted** — a comma precedes the claim (`According to Alice, X`);
4. **indirect speech** — a *closed class* of English reporting verbs frames the
   claim as content (`Alice said X`, `X, said Alice`).

Frames 1–3 are purely positional; frame 4 uses the minimal lexical knowledge
needed to recognise a report (`_REPORTING_VERB` regex: said, tells, reports,
according, etc.) — deliberately not an open-ended attribution-adjective denylist.

### Regression tests added

`tests/unit/test_answer_engine.py` — `test_engine_indirect_attribution_abstains`
(parametrised over 3 sources) proves both probe cases abstain:

| Source | Claim | Score | Engine result |
|---|---|---|---|
| `Alice said the launch is approved.` | `The launch is approved.` | 0.0 | INSUFFICIENT_EVIDENCE / CITATION_INVALID |
| `According to Alice, the launch is approved.` | `The launch is approved.` | 0.0 | INSUFFICIENT_EVIDENCE / CITATION_INVALID |
| `The launch is approved, said Alice.` | `The launch is approved.` | 0.0 | INSUFFICIENT_EVIDENCE / CITATION_INVALID |

Additionally, `test_engine_denied_reported_qualified_attribution_abstains`
covers clause-level negation (`denied`), third-party reporting (`reported`),
and hedged attribution (`qualified`) as abstention cases.

## Verification

- `pytest -q tests/unit/test_answer_engine.py` — **55 passed**.
- `PYTHONPATH=evals pytest -q tests/unit/test_graph_dataset_manifest.py` —
  **9 passed** (V2 tests; see `V2-G5-MANIFEST-REREVIEW-2.md` for open findings).
- `/root/ko-venv/bin/ruff check packages/answer/verification.py tests/unit/test_answer_engine.py`
  — **All checks passed** (fixed RUF002 EN-DASH in docstring).
- Direct probes confirm all four attribution frames score 0 and the Unicode span
  is `(11, 26)`.

V1 Q5 P1 is closed. Q9/product acceptance stays open until coordinator accepts.
This review does not accept V1 product completion or waive Q9, quality, or
release gates.
