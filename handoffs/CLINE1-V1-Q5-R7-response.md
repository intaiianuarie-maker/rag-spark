# CLINE1-V1-Q5-R7/R8 Response Handoff — P2 comma-scope fix (syntactic narrowing)

> Response to coordinator REQUEST CHANGES in `reviews/V1-Q5-REREVIEW-7.md`
> (and follow-up R8 finding). Closes the remaining P2 from R7.

---

## CHANGED FILES

| File | Change |
|---|---|
| `packages/answer/verification.py` | Narrowed Frame 3 (comma-fronted) and Frame 4 (indirect speech) to require syntactic connectivity; removed `note` from `_REPORTING_VERB`; added `_SUBORDINATOR` regex; updated docstring |
| `tests/unit/test_answer_engine.py` | Expanded `test_engine_comma_not_attribution_is_grounded` to 4 parametrised cases (added "In the note, ..." and "After Bob responded, ...") |

No review files modified. No merge, no deploy, no Q8/Q9 waiver.

---

## CHANGE

### `packages/answer/verification.py`

**Problem (R7/R8):** `_is_attributed_frame()` searched for any reporting verb
anywhere in the text before the claim, causing false abstentions:

- `In the note, the launch is approved.` — "note" is a noun in the verb class
- `After Bob responded, the launch is approved.` — "responded" is a verb in a
  subordinate clause, not attribution

**Fix — three scoped changes:**

1. **Removed `note` from `_REPORTING_VERB`** (line ~115): `note|notes|noted|noting`
   removed — same noun/verb ambiguity as `report` (already excluded).

2. **Added `_SUBORDINATOR` regex** (line ~129): matches words that introduce
   subordinate clauses / prepositional phrases before a comma:
   `after|although|as|because|before|even|if|in|on|since|though|unless|until|when|where|whether|while|whereas|wherever|whenever|however|therefore|despite|whereupon`

3. **Frame 3 narrowed** (line ~169): now requires a comma AND a reporting verb
   in the text before the last comma, AND the pre-comma text must NOT start with
   a subordinator:
   ```python
   if "," in before:
       last_comma_idx = before.rfind(",")
       pre_comma = before[:last_comma_idx]
       if _REPORTING_VERB.search(pre_comma) and not _SUBORDINATOR.match(pre_comma):
           return True
   ```

4. **Frame 4 narrowed** (line ~179): only checks the text after the last comma in
   `before` (i.e., immediately adjacent to the claim), so verbs in subordinate
   clauses are not mistaken for attribution:
   ```python
   _before_adj = before.rsplit(",", 1)[-1] if "," in before else before
   if _REPORTING_VERB.search(_before_adj) or _REPORTING_VERB.search(after):
       return True
   ```

This preserves the three attribution patterns from R7:
- `According to Alice, the launch is approved.` (Frame 3: comma + "according" before comma, not a subordinator)
- `Alice said the launch is approved.` (Frame 4: "said" immediately before claim, no comma)
- `The launch is approved, said Alice.` (Frame 4: "said" immediately after claim)

While rejecting the false positives:
- `On Friday, ...` (no reporting verb — subordinator "on" + no verb)
- `In the report/note, ...` ("note"/"report" removed from verb class)
- `After Bob responded, ...` ("After" is a subordinator; "responded" is in a subordinate clause)

### `tests/unit/test_answer_engine.py`

Expanded `test_engine_comma_not_attribution_is_grounded` from 2 to 4 parametrised
cases:

| Case | Added |
|---|---|
| `On Friday, the launch is approved.` | R7 |
| `In the report, the launch is approved.` | R7 |
| `In the note, the launch is approved.` | R8 |
| `After Bob responded, the launch is approved.` | R8 |

The existing `test_engine_indirect_attribution_abstains` (3 negatives) is
retained unchanged.

---

## EXACT TEST / RUFF RESULTS

```
$ cd "/root/code/Super Rag/knowledge-os"
$ /root/ko-venv/bin/ruff check packages/answer/verification.py tests/unit/test_answer_engine.py
All checks passed!

$ /root/ko-venv/bin/python3 -m mypy packages/answer
Success: no issues found in 7 source files

$ /root/ko-venv/bin/python3 -m pytest tests/unit/test_answer_engine.py -q
59 passed in 0.95s
```

---

## SCORE PROBES

Claim: `The launch is approved.`

### Negatives (attribution — should abstain, score 0.0)

| Source | Score |
|---|---|
| `Alice said the launch is approved.` | 0.0 |
| `According to Alice, the launch is approved.` | 0.0 |
| `The launch is approved, said Alice.` | 0.0 |
| `Alice challenged the assertion: the launch is approved.` | 0.0 |

### Positives (NOT attribution — should ground, score 1.0)

| Source | Score |
|---|---|
| `On Friday, the launch is approved.` | 1.0 |
| `In the report, the launch is approved.` | 1.0 |
| `In the note, the launch is approved.` | 1.0 |
| `After Bob responded, the launch is approved.` | 1.0 |

---

## RISKS

- **"report"/"note" excluded from verb class**: "Alice reported/ noted the claim is
  approved" (no colon, no comma) is no longer caught by Frame 4. Acceptable
  v1 interim trade: rare construction, conservative bias toward grounding.
- **Subordinator list is not exhaustive**: edge cases with unusual conjunctions
  may still false-positive. The closed class can be extended as needed.

## BLOCKERS

- **v1 merge — blocked on w4:p17** (AGENTS.md §2.6, only reviewer merges)
- **Q9/product acceptance — open** pending coordinator verification

Awaiting independent verification — no merge, no deploy.
