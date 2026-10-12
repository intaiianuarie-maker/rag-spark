# CLINE1-V1-Q1-R1 Handoff — V1 Q1: reject zero/invalid embeddings

> Durable session handoff per standing order (AGENTS.md §4).
> Agent: Cline (Backend) · resuming the in-flight `dev1` partial V1 Q1 edit.
> Target spec: `Super Rag/rag-fork-solution/COORDINATOR-NEXT-TASK-SPECS.md` §V1-Q1.
> Branch: `feat/v2-graph` (base commit `3be1cdc67eef96f9d24ca5434d3754f0ea4f17ee`).
> Uncommitted working-tree changes — not merged. Q1 does NOT close V1; Q8/Q9 remain open.

---

## TASK

`§V1-Q1: reject zero/invalid embeddings`. An embedding provider response with an
all-zero (or numerically invalid) vector must not be accepted as valid semantic
evidence — it carries no signal and would match every query under cosine
similarity, a manufactured vector that can produce false success.

Allowed files (touched ONLY these two this task):

- `packages/retrieval/adapters/infinity_embed.py`
- `tests/contracts/test_infinity_embedding_contract.py`

Required behavior (from §V1-Q1):

1. Reject a zero/near-zero vector with typed `EmbeddingProviderError` **before**
   returning any document **or** query vector.
2. Preserve dimension checks, ordering, batching, valid-vector values, and
   empty-batch behavior; never fabricate or silently skip invalid vectors.
3. If a batch contains any zero vector, fail the batch; return no partial success.

Required tests (from §V1-Q1): extend the fake-server contract tests to cover zero
vectors for both document and query calls; assert typed failure; keep valid
deterministic cases green. (Non-finite/NaN-Inf handling explicitly out of scope.)

## STATUS

**CLOSED / GREEN — verified, 3/3 acceptance commands pass.**

The `dev1` partial edit was already present in the working tree. This session
**preserved it** (no reset, no stash) and verified it satisfies behavior #1 and #2:
the zero-vector guard lives in `_embed`, so both `embed_query` and `embed_documents`
are covered, and the existing dimension/ordering/batching/empty paths are untouched.

This session added the one genuinely uncovered piece — a test for behavior #3
("fail the whole batch; return no partial success") using a **mixed** batch where a
valid vector precedes the zero one, proving the whole `embed_documents` call raises
and no partial vectors escape. `reviews/` was not edited; V1 Q8/Q9 were not closed.

## CHANGE

### `packages/retrieval/adapters/infinity_embed.py` — preserved unchanged this session

The working-tree diff vs HEAD is the prior `dev1` partial edit (not touched here).

| Area | Before (HEAD) | After (working tree) |
|---|---|---|
| Zero-vector guard | absent | `_is_zero_vector()` (L2 norm `< ZERO_VECTOR_TOLERANCE`, 1e-9) invoked in `_embed` before `vectors.append` |
| Error type on bad vector | — | `EmbeddingProviderError` |
| Coverage | — | applies to both `embed_query` and `embed_documents` (both call shared `_embed`) |
| Dimension / ordering / batching / empty-batch | existing | unchanged (guard placed after dimension handling, before append) |

### `tests/contracts/test_infinity_embedding_contract.py` — partial edit + this session's addition

| Area | Before (HEAD) | After (working tree) |
|---|---|---|
| Stub `inputs` extraction | parsed inline in the valid path | hoisted before the mode checks (so fake modes can use it) |
| Stub `zero_vector` mode | absent | returns `[0.0]*1024` for every input |
| Stub `zero_vector_last` mode | absent | valid vector per input except the last (all-zero) — added this session |
| `test_zero_vector_is_rejected` | absent | asserts `EmbeddingProviderError` ("zero-vector") for zero vectors in **both** `embed_documents` and `embed_query` (`dev1`) |
| `test_zero_vector_in_batch_fails_whole_batch` | absent | mixed batch [valid, valid, zero] → whole call raises; single batched request of 3 inputs, no partial result — added this session |
| Shared contract `check_embedding_provider_contract` | asserts non-zero doc + query vectors (Q1) | still green via `test_adapter_satisfies_embedding_contract` |

Design note: because the rejection is inside `_embed` (the single method that both
public entry points call), a zero vector anywhere in a batch raises before any vector
is appended/returned, which is what behavior #3 ("return no partial success") requires.

## VERIFY

Run from `/root/code/Super Rag/knowledge-os`:

```
$ /root/ko-venv/bin/python3 -m pytest tests/contracts/test_infinity_embedding_contract.py -q
.................                                                        [100%]
17 passed in 10.34s
pytest_exit=0
```

```
$ /root/ko-venv/bin/ruff check packages/retrieval/adapters/infinity_embed.py tests/contracts/test_infinity_embedding_contract.py
All checks passed!
ruff_exit=0
```

```
$ /root/ko-venv/bin/python3 -m mypy packages/retrieval
Success: no issues found in 9 source files
mypy_exit=0
```

`pythonpath` (packages / apps/api / apps/worker / ".") is configured in
`pyproject.toml`, so `/root/ko-venv/bin/python3` (system Python 3.12) resolves
`retrieval` under `packages/` and runs the async tests via `pytest-asyncio 1.4.0`.

## SCOPE / STOP CONDITIONS (per §V1-Q1)

- Touched ONLY `packages/retrieval/adapters/infinity_embed.py` (preserved) and
  `tests/contracts/test_infinity_embedding_contract.py` (extended).
- `reviews/` not edited. No callers, migrations, product tables, graph code, APIs,
  compose files, or evaluation labels were altered.
- Non-finite (NaN/Inf) handling intentionally NOT added — out of this bounded task
  (`§V1-Q1`: "Non-finite handling is outside this bounded task").
- No merge, no branch switch, no deploy, no V1-completion claim. V1 Q8 and Q9 remain
  open. This task does not satisfy live deletion/isolation, end-to-end acceptance, or
  merge/release gates.

## NEXT ACTION / UNBLOCKER

- Next action: independent review of the two Q1 files (the `_is_zero_vector` tolerance
  and the mixed-batch `test_zero_vector_in_batch_fails_whole_batch`), then commit on
  `feat/v2-graph`.
- No merge until coordinator/w4:p17 reviews — per standing gate, only the reviewer
  merges to `main`.
