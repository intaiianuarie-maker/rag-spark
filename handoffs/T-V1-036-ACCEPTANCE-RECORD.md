# T-V1-036 — V1 acceptance record

**Run date:** 2026-10-13 · **HEAD:** `5369d97` · **box:** vmi3584180
**Executed by:** code2, against `handoffs/T-V1-036-RUNBOOK.md` in its authored order
**Verdict: CONDITIONAL ACCEPTANCE** — every gate threshold met, 6 of 7 steps fully green,
one **new product red** found and filed (not in the known-failure annex).

Transcripts (verbatim, on disk): `handoffs/T-V1-036-STEP1..STEP7b-*.log`

---

## 1. Per-step result

| # | step | result | numbers | duration |
|---|---|---|---|---|
| 1 | unit + contract | **PASS** | `1350 passed, 0 failed` | 103.82 s |
| 2a | Q9 bench | **PASS** | `GATE: PASS` — groundedness **0.9888** (≥0.90), citation validity **1.0000** (==1.0), abstention **1.0000** (≥0.95), `leaked=0` | 1.11 s |
| 2b | Q9 gate test | **PASS** | `3 passed` | 1.90 s |
| 3 | web e2e (`RUN_WEB_E2E=1`) | **PASS** | `2 passed`, **zero skips** (asserted) | 42.27 s |
| 4 | ingestion trio | **PASS** | `19 passed, 2 xfailed` | 41.00 s |
| 5 | dedup / versions | **PASS** | `18 passed` | 66.85 s |
| 6 | crash recovery | **FAIL (1 of 8)** | `1 failed, 7 passed` — the **6 known-reds healed**; 1 **NEW** red | 107.36 s |
| 7 | reconciliation (dry run) | **PASS** | 2041 points, 1927 matched, 114 orphans, 627 pointless chunks | — |
| 7b | post-run `/ready` | **PASS** | `ready:true`, every dependency reachable — **no degradation window** | — |

Q9 per-slice, identical to the T-V1-076 baseline:
`en 1.0000/1.0000 · ro 1.0000/1.0000 · pt 0.9853/1.0000 · tables 1.0000/1.0000 · multi 0.9586/1.0000 · OVERALL 50/0.9888/1.0000 · unanswerable abstention 1.0000`

## 2. Gates — every threshold met

| gate | required | measured | |
|---|---|---|---|
| groundedness | ≥ 0.90 | **0.9888** | ✅ |
| citation validity | == 1.00 | **1.0000** | ✅ |
| abstention (unanswerable) | ≥ 0.95 | **1.0000** | ✅ |
| factual | ≥ 98% | **not measured** | ⚠️ no runner (annex F7) |
| Recall@20 | ≥ 95% | **not measured** | ⚠️ no runner (annex F7) |

**This record therefore does not evidence RP-02's five targets — it evidences three.**
Stated plainly rather than letting a green 3-of-5 read as 5-of-5.

## 3. The new red (not in annex F1–F10) — FILED, not fixed

`tests/integration/test_crash_recovery.py::test_redelivery_after_delete_is_fenced_not_resurrected`

```
assert 'INGESTION_ERROR' == 'DOCUMENT_DELETED'
```

* **The safety property holds.** `final_status == "failed"` passed, `drained=True`,
  `verdict='PASS'`, no resurrection, no writes. Only the **disclosed code label** is wrong.
* **Mechanism (traced, not guessed).** The mid-cycle delete now also **purges the
  originals** — the transcript shows `document_originals_purged … objects_removed: 1`. On
  restart, `load_original_bytes` hits `OriginalMissing` and raises
  `IngestPipelineError("INGESTION_ERROR", "original object is missing from object storage")`
  (`ingest_pipeline.py:~633`, `retryable=False`) **before** the PRE-WRITE generation fence
  that would have emitted `FENCE_DELETED = "DOCUMENT_DELETED"` (`:102`, raised at `:154`).
  So the originals guard, added by the T-039/T-072 line of work, **fires ahead of the
  delete fence**.
* **Why it matters beyond a label.** `DOCUMENT_DELETED` ("this document was deleted while
  the job was running") and `INGESTION_ERROR` ("the original object is missing from object
  storage") are both terminal, but at the code level a **delete is now indistinguishable
  from storage rot**. That is a disclosure regression in exactly the place the acceptance
  gate exists to catch.
* **Follow-up filed** (product code — out of this ticket's allowlist): decide precedence.
  Either the delete fence wins when the version's document is tombstoned, or
  `OriginalMissing` maps to `DOCUMENT_DELETED` in that case. Not fixed here.

## 4. Known-failure annex — what actually happened

| id | expected | observed |
|---|---|---|
| **F1** crash 6 failures | 6 | **HEALED — 0.** `wait_stalled` cross-cycle race no longer fires (T-V1-066 per-cycle teardown + T-V1-071). The historical ~348 s cost is now 107 s |
| **F2** 373 non-authentic vectors | present | **unchanged** — corpus byte-stable (2041 points) |
| **F3** ~114 orphans / 546 pointless | present | orphans **unchanged at 114** (67/7/0/40); pointless chunks **546 → 627 (+81)** |
| **F4** web e2e cold-build timing | varies | 42.27 s (incremental) — within expectation |
| **F5** 1 FTS row survives delete | filed | not re-observed in this run |
| **F6** acceptance_5 multilingual | unverified | not exercised by this runbook (no acceptance-probe step) |
| **F7** factual / Recall@20 no runner | unmeasured | **still unmeasured** |
| **F8** citation_validity partially vacuous | known | unchanged — `all([])` still `True` |
| **F9** corpus hash-pinned | passes | **passes** — no `SliceIntegrityError` in step 2 |
| **F10** corpus is test debris | known | unchanged |

**T-V1-078's filed foreign-red is HEALED**: `test_originals_object_store.py` → **`5 passed`**
(was 1 failed). T-V1-072's fix landed and the `t1072` rows now carry `storage_key`.

## 5. Isolation — the strongest result in this run

**Live `knowledge` is byte-identical before and after the entire acceptance run:**

| | pre-run | post-run |
|---|---|---|
| `knowledge` points | **2041** | **2041** |
| orphans (67/7/0/40) | 114 | 114 |

Every live suite wrote to **scratch collections** and dropped them (visible in the
transcripts: `dropped scratch collection knowledge_test_security_…`, `…knowledge_test_dedup_…`).
T-V1-071's isolation works. **The one thing that did grow is Postgres** — pointless chunks
546 → 627 — so PG accumulates test rows while Qdrant does not.

## 6. Reporting caveats that travel with this record

1. **Oracle, not live retrieval.** Q9 is a gold-context oracle (museR's standing ruling):
   a green Q9 says nothing about embeddings or the live index.
2. **The corpus cannot evidence retrieval quality** (F10): 102 of 2034 points are
   authentic bge-m3; the rest are test fixtures. Acceptance #1/#5 could go green without
   proving real multilingual retrieval.
3. **`citation_validity` is partially vacuous** (F8) and **abstention measures the claim
   gate's refusal**, not generator-side abstention (T-V1-076).
4. **web e2e's 42.27 s was an incremental build**, not a CI cost.
5. The box was **not idle** during this run — load average 5.70 on 4 cores at start (runbook
   E3 asks for a free box). Nothing timed out and no step exceeded 2× its budget, but the
   durations are not idle-box numbers.