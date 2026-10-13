# T-V1-036 — V1 acceptance runbook

**Status:** authored, and **executed on 2026-10-13** — see `handoffs/T-V1-036-ACCEPTANCE-RECORD.md`. This file is the ordered plan the T-036 run
phase follows. Every number below is **sourced** from a committed record, cited inline.
Nothing here was measured while writing it — where a duration was never recorded, it is
marked **NOT RECORDED** rather than estimated into a fake figure.

Repo: `Super Rag/knowledge-os` · interpreter: `/root/ko-venv/bin/python3` (3.14.4)
Author: code2, T-V1-079 (docs only) · Card: `../rag-fork-solution/tasks/MICRO-TICKETS-V1.md:536`

---

## 1. Environment preconditions

| # | precondition | how to check | notes |
|---|---|---|---|
| E1 | Stack up, all deps reachable | `curl -s localhost:8000/ready` | record the full dependency block in the transcript; `ready:true` with every dependency `reachable:true` (T-V1-070 observed exactly this) |
| E2 | Embeddings serving | `/ready` → `infinity:true` | bge-m3 1024-d on `:7997`; Q9's *live* leg and any ingest-touching suite need it |
| E3 | **Box free** — no reindex, no foreign model server | `uptime`, `free -g`, `ps` | **load average must be low.** At load **8.04** (T-V1-077) a 84 s web build still passed, but that is not the margin to plan on. The llama-server lesson: a foreign model process starves everything and flips `/ready` to degraded |
| E4 | Crash-failure forensics armed | `export T021_KEEP_MARKERS=1` | **only when a crash test fails.** T-V1-067's default sweep deletes the worker logs — which is how T-V1-064 found its root cause. Set it, re-run that one test attached, read the logs, then unset it |
| E5 | Transcript capture on | `… \| tee /tmp/opencode/<ticket>-<step>.txt` | closes the T-V1-061 FLAG-6 provenance gap: the 0.9888 figure previously existed only as a handoff table, never as a log |
| E6 | Attached runs only | never `&`, never detached | a detached suite run is what orphaned 29 workers / ~4 GB (T-V1-054) |

**Writes to the live `knowledge` collection are expected** — the integration suites ingest
into it. That is why **T-V1-073's reconciliation must be re-run after the acceptance run**,
not before: a pre-run prune would be undone by the suites.

---

## 2. Ordered command list

Ordering rationale: **hermetic gates first** (so a cheap, unambiguous failure does not
consume a 348 s crash run), **heaviest last**. Every command runs from the **repo root**
unless stated, attached in the foreground.

### Step 1 — unit + contract gate (hermetic, no services)

```bash
/root/ko-venv/bin/pytest tests/unit tests/contracts -q
```
* **Recorded result:** `1252 passed, 0 failed` (T-V1-057).
* **Duration: 103.82 s** (first measured on the 2026-10-13 acceptance run; the runbook
  previously said NOT RECORDED).
* Fails on: anything not in §4. This is the gate most likely to stay green.

### Step 2 — Q9 benchmark gate (hermetic: gold-context oracle, no services)

```bash
# 2a. the bench itself — capture the transcript
/root/ko-venv/bin/python3 evals/q9_run_benchmark.py | tee /tmp/opencode/t036-q9.txt
# 2b. the gate test that wraps it
/root/ko-venv/bin/pytest tests/integration/test_benchmark_gate.py -q
```
* **Recorded result (T-V1-076):**
  ```
  en 10 1.0000 1.0000 | ro 10 1.0000 1.0000 | pt 10 0.9853 1.0000
  tables 10 1.0000 1.0000 | multi 10 0.9586 1.0000
  unanswerable 10 n/a n/a  abstention=1.0000 leaked=0
  OVERALL 50 0.9888 1.0000
  GATE: PASS — groundedness 0.9888 (>= 0.9), citation_validity 1.0000 (== 1.0), abstention 1.0000 (>= 0.95)
  ```
* `test_benchmark_gate.py` = **3 tests** (T-V1-076 ran it with two other files: 37 total).
* **Duration:** `1.11 s` (2a) and `1.90 s` (2b) — first measured on the 2026-10-13 run.
* The slice file is **hash-pinned** (T-V1-075): drift raises `SliceIntegrityError` from
  `load_slices()`. A pin error is **not** a product failure — see §4 F9.

### Step 3 — web e2e (opt-in; needs node/npm + a production build)

```bash
RUN_WEB_E2E=1 /root/ko-venv/bin/pytest tests/integration/test_web_e2e.py -q
```
* **Recorded result:** `2 passed in 84.42s` (T-V1-077); **`2 passed in 42.27s`** on the
  2026-10-13 acceptance run (incremental build).
* **Assert zero skips** for this file — with `RUN_WEB_E2E` unset it reports
  **`2 skipped in 0.04s` and exits 0**, which is a skip indistinguishable from a pass.
  Run with `-rs` and confirm no `SKIPPED` line for `test_web_e2e.py`.
* **84.42 s was an INCREMENTAL build** (`.next/` already existed). A cold build is
  materially slower — see §4 F4.

### Step 4 — ingestion trio (live: embed + Qdrant + PG)

```bash
/root/ko-venv/bin/pytest tests/integration/test_ingestion_jobs.py \
                     tests/integration/test_knowledge_reads.py \
                     tests/integration/test_security_placeholders.py -q
```
* **Recorded result:** `19 passed, 2 xfailed` (dev1 T-V1-071).
* The two T-049-era red tests inside it are **confirmed green**: `test_pipeline_reaches_ready_with_chunks`
  **9.82s** and `test_charset_hint_round_trip` **1.67s** (T-V1-070, `2 passed in 15.27s` / `13.13s`).

### Step 5 — dedup / versions (live)

```bash
/root/ko-venv/bin/pytest tests/integration/test_dedup_versions.py -q
```
* **Recorded result:** `17 passed`, two runs (dev1 T-V1-071; also T-V1-059).
* This suite's live writes go to a **scratch** collection since dev1's T-V1-071.

### Step 6 — crash recovery (heaviest; live)

```bash
/root/ko-venv/bin/pytest tests/integration/test_crash_recovery.py -q
```
* **Recorded result:** `7 failed, 1 passed in 347.75s` (T-V1-064), of which T-V1-065 cleared
  `test_crash_during_parse` (`1 passed in 70.27s`) and one more was attributed to T-V1-059.
  **Expected today: 6 failures** — see §4 F1.
* **~348 s is the recorded cost.** On a contended box it is worse; budget accordingly.
  The 2026-10-13 acceptance run completed this step in **107.36 s** — the six
  long-standing `wait_stalled` failures no longer fire, so the historical 348 s no
  longer describes this file.
* If it fails: set `T021_KEEP_MARKERS=1` (E4) and re-run that one test.

### Step 7 — post-run reconciliation (live, read-only)

```bash
/root/ko-venv/bin/python3 scripts/reconcile_points_chunks.py --collection knowledge | tee /tmp/opencode/t036-recon.txt
```
* Dry run by default; **never** pass `--apply` here. Last recorded: 2041 points, 114 orphans
  (67 superseded / 7 tombstoned / 0 no-PG-row / 40 other), 546 pointless chunks (T-V1-073).
* Re-run **after** the suites, because the suites ingest.

---

## 3. Thresholds (every value sourced)

| gate | value | source (quoted) |
|---|---|---|
| groundedness | **≥ 0.90** | `evals/q9_run_benchmark.py:57` → `GROUNDEDNESS_GATE = 0.90` |
| citation validity | **== 1.00** | `evals/q9_run_benchmark.py:58` → `CITATION_GATE = 1.0` |
| abstention (unanswerable slice) | **≥ 0.95** | `evals/q9_gates.py:43` → `ABSTENTION_TARGET = 0.95`, wired as `ABSTENTION_GATE` at `q9_run_benchmark.py:60` |
| factual | **≥ 98%** | T-036 card, `MICRO-TICKETS-V1.md:549` → "factual ≥98%". **Not yet gated** — see F7 |
| Recall@20 | **≥ 95%** | card line 550 → "Recall@20 ≥95%". **Not yet gated** — see F7 |
| per-claim support cutoff | 0.90 | `evals/q9_gates.py:36` → `FACTUAL_SUPPORT_CUTOFF = 0.90` (**harness choice**, not a card value) |
| `RECALL_AT_K` | 20 | `evals/q9_gates.py:39` |

**Thresholds must not move.** The five answerable slices' numbers are pinned as constants in
`tests/unit/test_q9_gates.py` (`BASELINE_ANSWERABLE`, `BASELINE_OVERALL`) and a change to
them fails the suite. That is deliberate: it is what stops a green gate from being produced
by relaxing a bar.

---

## 4. Known-failure annex — red-that-is-known vs red-that-is-new

Anything not listed here is **NEW** and must be triaged, not absorbed.

| id | what is red / notable | ticket | expected state | if it differs |
|---|---|---|---|---|
| **F1** | crash suite: **6 failures**, all `wait_stalled` 45 s timeouts, never a product assertion | T-V1-064, T-V1-066 | 6 failures | **NEW.** Do not absorb as the known race |
| **F2** | **373 non-authentic vectors** in `knowledge` (flat unit vector `0.03125 = 1/√1024`, not embeddings of their text) | T-V1-069, T-V1-071 | present; re-index pending | count may move as suites run |
| **F3** | **~114 orphan points** (67 superseded / 7 tombstoned / 0 no-PG-row / **40 unexplained**), 546 pointless chunks | T-V1-072, T-V1-073 | present | the **40 "other"** are unexplained and were never diagnosed |
| **F4** | web e2e **cold-build timing** — the recorded 84.42 s was incremental | T-V1-077 | green, duration varies | a cold build will exceed 84.42 s; that is **not** a regression |
| **F5** | **1 FTS row survives a delete** | T-V1-058 | filed, unfixed | — |
| **F6** | `test_acceptance_5_multilingual_cross_language_retrieval` was red for missing live embeddings | T-V1-036 probe | **unverified since embeddings cleared** | must be re-checked, asserted **neither way** |
| **F7** | **factual / Recall@20 have no end-to-end runner** — harnesses exist and are unit-tested, but no command reports them | T-V1-076 | unmeasured | the record **cannot** claim RP-02's five targets yet |
| **F8** | `citation_validity` is **partially vacuous**: `all([])` is `True`, so a record with no surviving citation scores 1.0 | T-V1-076 | known, unchanged | 9 of 10 `en` records do produce a real citation |
| **F9** | slice corpus is **hash-pinned**; `SliceIntegrityError` on drift | T-V1-075 | passes | a pin error is a **corpus** failure, not a product failure |
| **F10** | the **live corpus is integration-test debris** — 102 of 2034 points are authentic bge-m3, the rest are test fixtures | T-V1-061b | known | acceptance probes can go green **without** evidencing real retrieval quality |

---

## 5. Reporting rules

1. **Every step gets a transcript file**, not a summary line. Cite the path in the handoff.
2. **Report the defensible number, not the flattering one.** If a gate is skipped, green by
   default, or vacuously 1.0, say which — see F8.
3. **Oracle vs actual-output stay separated.** Q9 is a gold-context oracle; the retrieval
   leg is not. A green Q9 says nothing about embeddings (museR's standing ruling).
4. **Never fix a gate inside the run phase.** A failure gets a ticket, not an edit.
5. **After the run:** re-run T-V1-073's reconciliation and record the delta, because the
   suites ingest into the same collection the record is describing.