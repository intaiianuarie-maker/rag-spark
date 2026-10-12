# CLINE1-V1 Handoff — Super RAG v1

> Durable session handoff per standing order. Survives /clear.
> Agent: Cline 1 (Backend Engineer) · Supervisor: Codex w4:p15

---

## TASK

Complete Super RAG v1 milestone per `rag-fork-solution/V1-COMPLETION-PLAN.md`:
make v1 (M0–M8) work end-to-end, pass Q0–Q10 gates. No new features; close
live-path gaps from 2026-10-09 review.


## STATUS

**READY FOR RE-REVIEW — all Codex REQUEST CHANGES findings fixed.**
26 tests pass (23 repo + 3 reviewer). Ruff E501 clean. docker-compose binding
accepted by Codex. Q8 dataset and streaming endpoint deferred per supervisor notes.


## CHANGE (files I own / have modified)

| File | Change |
|------|--------|
| `packages/answer/engine.py` | All 5 return paths now refuse raw prose: parse fallback → INSUFFICIENT_EVIDENCE; empty claims → NO_CLAIMS; zero evidence/refusal → abstention msg (was raw answer_text); all-claims-verified → render verified claim text (was raw answer_text); partial → verified claim text only. trace_id moved before parse. Docstring updated. E501 wrapped. |
| `tests/unit/test_answer_engine.py` | Updated `test_engine_unparseable_output_falls_back` + `test_engine_returns_verified_answer` + `test_engine_partial_claims_marks_partial_status` for new rendering. Added 5 edge-case tests (empty claims, malformed prose, no evidence, trace_id in parse fallback, trace_id in no-claims). Added 3 reviewer regression cases (`test_unsupported_prose_never_returned` parametrized: no_evidence, all_claims_valid, refusal_phrase). Added `import json`. |
| `docker-compose.yml` | `infinity-rerank` port `7997:7997` → `127.0.0.1:7997:7997` (P0 security) |
| `reviews/2026-10-09-super-rag-review/finding-01-infinity-rerank-port.md` | P0 finding doc |

**Files I did NOT touch (per instructions):**
- No edits to `packages/graph/` (Cline 2 v2 code, untracked, 4 files)
- No branch switch
- No pushes or remote merges

## WHY

Codex w4:p15 review (2026-10-09) REQUEST CHANGES with 4 findings:
1. **High**: `engine.py:164,180` returns raw model answer after filtering claims —
   unsupported sentences visible with PARTIAL status; empty claims marked ANSWERED.
   Fix: render only verified claims or abstain.
2. **High**: `docker-compose.yml:161` exposed infinity-rerank on all interfaces.
   Fix: bind to 127.0.0.1.
3. **Deferred**: storage_url schema change — incomplete without migration + working path.
4. **Process**: use `/root/ko-venv` for test validation; separate Q8 dev/holdout data.

Satisfies: DECISIONS.md "No unverified claims in answer", AGENTS.md §4 "Citation Authenticity",
and v1 security policy "backing services loopback only".

## REUSED

- pydantic (MIT): typed models
- FastAPI (MIT): API routing
- SQLAlchemy (MIT): async PG persistence
- qdrant-client (Apache-2.0): vector store
- httpx (MIT): HTTP for provider adapters
- boto3 (Apache-2.0): SeaweedFS S3 adapter
- pypdf/pdfplumber (MIT/BSD): PDF extractors
- beautifulsoup4 (MIT)/lxml (BSD): HTML parsing
- lingua-language-detector (Apache-2.0): language detection
- LiteLLM (MIT): model gateway passthrough
- Git (GPL, CLI only): git connector subprocess
- PostgreSQL FTS (PostgreSQL License): lexical store

## BUILT

- `AnswerEngine.answer()`: custom product glue composing retrieval → prompt → LLM → parse → verify → render.
- Claim rendering policy: **every** successful/partial factual return path renders verified claim text (`" ".join(claim.text for claim in verified)`) instead of the model's raw `answer` field. Zero-evidence and refusal paths return a trusted abstention message. trace_id captured before parse so it is always attached. Docstring updated with grounded-rendering guarantee.
- 8 new test cases in `test_answer_engine.py` + 3 parametrized reviewer regression cases (`test_unsupported_prose_never_returned`).
- Claim rendering policy: when partial claims survive verification, answer text is synthesized from verified claim text only (`" ".join(claim.text for claim in verified)`).
- 5 new negative test cases for answer engine edge cases.

## VERIFY

Run from repo root with ko-venv:

```
$ cd "/root/code/Super Rag/knowledge-os"
$ PYTHONPATH="packages:apps/api:apps/worker:." \
  APP_ENV=test MIGRATIONS_ON_BOOT=false LOG_JSON=false \
  /root/ko-venv/bin/python3 -m pytest tests/unit/test_answer_engine.py -v

23 passed in 1.88s
```

Reviewer regression test:
```
$ PYTHONPATH="packages:apps/api:apps/worker:tests/fixtures:tests/unit:." \
  APP_ENV=test MIGRATIONS_ON_BOOT=false LOG_JSON=false \
  /root/ko-venv/bin/python3 -m pytest /tmp/test_cline1_rereview.py -v

3 passed, 1 warning in 1.26s
```

Combined (26 tests):
```
$ PYTHONPATH=... /root/ko-venv/bin/python3 -m pytest \
  tests/unit/test_answer_engine.py /tmp/test_cline1_rereview.py -v
26 passed in 6.19s
```

Ruff lint:
```
$ /root/ko-venv/bin/python3 -m ruff check packages/answer/engine.py tests/unit/test_answer_engine.py
All checks passed!
```

```
$ /root/ko-venv/bin/python3 -c "import yaml; yaml.safe_load(open('docker-compose.yml')); print('YAML valid')"
YAML valid
```

## RISKS

- **Abstention may reduce answer rate**: models returning unverified prose now
  get INSUFFICIENT_EVIDENCE instead of a plausible-sounding answer. Intentional.
- **Empty-claims regression**: if the model consistently returns `{"answer": "...", "claims": []}`,
  all answers will be INSUFFICIENT_EVIDENCE. Monitor after deploy.
- **No psycopg[binary] in global env**: lexical regression tests require ko-venv.
- **Docling**: available in ko-venv but heavy (5.82s for 2 tests).
- **Q8 datasets**: NOT expanded — deferred per supervisor note. Planned: separate
  `evals/golden_smoke.jsonl` (dev) from `evals/golden_holdout.jsonl` (holdout) with real source labels.

## UNRESOLVED REVIEW FINDINGS

| # | Finding | Status | Resolution |
|---|---------|--------|-----------|
| 1 | engine.py:135 — EVIDENCE_MISSING returns raw answer_text | **FIXED** | Returns trusted abstention msg |
| 2 | engine.py:201 — ANSWERED path returns raw answer_text | **FIXED** | Renders `" ".join(claim.text for claim in verified)` |
| 3 | E501 on PARTIAL reason line | **FIXED** | Wrapped to multi-line |
| 4 | Zero-evidence echoes unsupported prose | **FIXED** | Test: `test_unsupported_prose_never_returned[no_evidence]` |
| 5 | Refusal phrase echoes unsupported prose | **FIXED** | Test: `test_unsupported_prose_never_returned[refusal_phrase]` |
| 6 | All-claims-valid returns unrelated unverified answer | **FIXED** | Test: `test_unsupported_prose_never_returned[all_claims_valid]` |
| 7 | infinity-rerank port binding | **FIXED** | `127.0.0.1:7997:7997` — accepted by Codex |
| 8 | storage_url schema change | **DEFERRED** | Requires migration + SeaweedFS integration |
| 9 | Q8 dataset expansion | **DEFERRED** | Example count alone is not a release gate |
| 10 | /ask streaming endpoint | **DEFERRED** | Route-level SSE not started |

## BLOCKERS

None. All Codex REQUEST CHANGES findings fixed and verified locally.

## NEXT STEPS

1. ✅ Fix all raw-prose return paths in engine.py — DONE
2. ✅ Fix infinity-rerank loopback binding — DONE (Codex accepted)
3. ✅ Wrap E501 reason line — DONE
4. ✅ Add 3 reviewer regression cases — DONE (merged into repo)
5. ⬆️ Awaiting Codex re-review approval — pending
