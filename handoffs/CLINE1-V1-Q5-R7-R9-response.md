# CLINE1-V1-Q5-R7/R8/R9 Response Handoff — P2 full syntactic narrowing

> Response to coordinator REQUEST CHANGES in `reviews/V1-Q5-REREVIEW-7.md`,
> `reviews/V1-Q5-REREVIEW-8.md`, and `reviews/V1-Q5-REREVIEW-9.md`.
> P1 (R5), P2 (R7), P2 (R8), and P2 (R9) are all resolved.

---

## CHANGED FILES

| File | Change |
|---|---|
| `packages/answer/verification.py` | Restored `report`/`note` to `_REPORTING_VERB`; added `_SUBORDINATOR` regex; narrowed Frame 3 (subordinator exclusion) and Frame 4 (first-token proximity for both `before` and `after`) |
| `tests/unit/test_answer_engine.py` | Added 5 new regression cases: 2 attribution negatives (`Alice reported...`, `Alice noted...`), 3 post-claim verb positives (`, and Bob responded`, `after Bob responded`, `; Bob responded by email`) |
| `handoffs/CLINE1-V1-Q5-R7-R9-response.md` | This file |

No review files modified. No merge, no deploy, no Q8/Q9 waiver.

---

## CHANGE DETAILS

### `packages/answer/verification.py`

**1. Restored `report`/`note` in `_REPORTING_VERB`** (lines ~116-117):
```
# BEFORE (R8 — too narrow, missed direct attribution):
r"explain|explains|explained|explaining|"
r"suggest|suggests|suggested|suggesting|"

# AFTER (verbs restored for direct attribution detection):
r"explain|explains|explained|explaining|note|notes|noted|noting|"
r"suggest|suggests|suggested|suggesting|report|reports|reported|reporting|"
```

Noun false positives ("In the report, ...", "In the note, ...") are prevented
by the `_SUBORDINATOR` check in Frame 3 (pre-comma text starting with "In")
and the proximity check in Frame 4.

**2. `_SUBORDINATOR` regex** (line ~135): matches subordinating conjunctions
and prepositions at the start of pre-comma text:
```python
r"^\s*(?:after|although|as|because|before|even|if|in|on|since|though|"
r"unless|until|when|where|whether|while|whereas|wherever|whenever|"
r"however|therefore|despite|although|whereupon)\b"
```

**3. Frame 3 (comma-fronted) — narrowed with subordinator exclusion** (line ~172):
```python
if "," in before:
    last_comma_idx = before.rfind(",")
    pre_comma = before[:last_comma_idx]
    if _REPORTING_VERB.search(pre_comma) and not _SUBORDINATOR.match(pre_comma):
        return True
```
- "According to Alice, X" → pre_comma = "According to Alice", starts with "According" (not a subordinator) → attribution ✓
- "In the report, X" → pre_comma = "In the report", starts with "In" (subordinator) → NOT attribution ✓
- "After Bob responded, X" → pre_comma = "After Bob responded", starts with "After" (subordinator) → NOT attribution ✓

**4. Frame 4 (indirect speech) — narrowed with first-token proximity** (line ~184):
```python
_before_adj = before.rsplit(",", 1)[-1] if "," in before else before
_after_stripped = after.lstrip(" \t,;:." + _QUOTE_CHARS + "!?\u2014-")
_after_tokens = _after_stripped.split()
_after_first = _after_tokens[0] if _after_tokens else ""
if _REPORTING_VERB.search(_before_adj) or \
        (_after_first and _REPORTING_VERB.search(_after_first)):
    return True
```
- `before`: only checks text after the last comma (so "After Bob responded, X" → empty text → no match)
- `after`: only checks the first token (so ", and Bob responded" → first token "and" → no match; ", said Alice" → first token "said" → match)

### `tests/unit/test_answer_engine.py`

**Added 2 attribution negatives** to `test_engine_indirect_attribution_abstains`:
- `Alice reported the launch is approved.` → 0.0 / INSUFFICIENT_EVIDENCE
- `Alice noted the launch is approved.` → 0.0 / INSUFFICIENT_EVIDENCE

**Added 3 positive controls** in new `test_engine_postclaim_verb_not_attribution_is_grounded`:
- `The launch is approved, and Bob responded.` → 1.0 / ANSWERED/HIGH
- `The launch is approved after Bob responded.` → 1.0 / ANSWERED/HIGH
- `The launch is approved; Bob responded by email.` → 1.0 / ANSWERED/HIGH

All 4 existing positive controls and 3 existing attribution negatives are
retained unchanged and passing.

---

## EXACT TEST / RUFF RESULTS

```
$ cd "/root/code/Super Rag/knowledge-os"
$ /root/ko-venv/bin/ruff check packages/answer/verification.py tests/unit/test_answer_engine.py
All checks passed!

$ /root/ko-venv/bin/python3 -m mypy packages/answer
Success: no issues found in 7 source files

$ /root/ko-venv/bin/python3 -m pytest tests/unit/test_answer_engine.py -q
64 passed in 1.04s
```

---

## SCORE PROBES

Claim: `The launch is approved.`

### Negatives (attribution — expect 0.0)

| Source | Score | Status |
|---|---|---|
| `Alice said the launch is approved.` | 0.0 | OK |
| `According to Alice, the launch is approved.` | 0.0 | OK |
| `The launch is approved, said Alice.` | 0.0 | OK |
| `Alice challenged the assertion: the launch is approved.` | 0.0 | OK |
| `Alice reported the launch is approved.` | 0.0 | OK (R9) |
| `Alice noted the launch is approved.` | 0.0 | OK (R9) |

### Positives (NOT attribution — expect 1.0)

| Source | Score | Status |
|---|---|---|
| `On Friday, the launch is approved.` | 1.0 | OK |
| `In the report, the launch is approved.` | 1.0 | OK |
| `In the note, the launch is approved.` | 1.0 | OK |
| `After Bob responded, the launch is approved.` | 1.0 | OK |
| `The launch is approved, and Bob responded.` | 1.0 | OK (R9) |
| `The launch is approved after Bob responded.` | 1.0 | OK (R9) |
| `The launch is approved; Bob responded by email.` | 1.0 | OK (R9) |

---

## NEXT

Awaiting independent verification by w4:p17. No merge, no deploy, no Q8/Q9 waiver.
