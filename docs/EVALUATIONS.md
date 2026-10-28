# Evaluation datasets and runs

All endpoints authenticate the caller and check document-read access to the
selected workspace. Dataset/run IDs from another workspace return 404.

- `POST /v1/workspaces/{workspace_id}/eval/datasets`: submit `name` and golden
  `examples` (question, expected_answer, expected_sources, tags).
- `GET /v1/workspaces/{workspace_id}/eval/datasets` and `/datasets/{id}`:
  retrieve saved inputs.
- `POST /v1/workspaces/{workspace_id}/eval/runs`: supply `dataset_id`, or inline
  `name` and `examples`. Optional `config` selects `mode` (hybrid, semantic,
  keyword) and `top_k` (1–100). Results are stored before responding.
- `GET /v1/workspaces/{workspace_id}/eval/runs` and `/runs/{id}`: retrieve saved
  results, scores, dataset/config snapshot and reported accounting.
- `POST /v1/workspaces/{workspace_id}/eval/runs/{id}/replay`: recompute ranking
  metrics and accounting from the saved ID/counter snapshot, without contacting
  the corpus or any provider. Faithfulness reuses the recorded scorer output.
  Replayed usage is the original run's measurement, not additional provider
  spend. The new record identifies its source with `replay_of`.
- Compatibility routes: `POST /v1/evaluations/run` adds `workspace_id` to the
  same body; `GET /v1/evaluations/{run_id}?workspace_id=...` uses the same ACL.

Runs include a canonical SHA256 of the examples and a complete golden dataset
snapshot and per-question replay ID lists/counters. Snapshot replay reproduces
scores without source text or an external index. Repeating live retrieval requires the
same underlying corpus/index and provider versions; a configuration snapshot
does not freeze a mutable external index.

For scoring existing traces, supply `traces` in dataset order (exactly one per
example). Each trace accepts `doc_ids`, optional `cited_doc_ids`, `cost` in USD,
`tokens`, `latency_ms`, and optional transient `context`/`answer` for lexical
faithfulness. Usage values must be finite and nonnegative. The response sums
the reported accounting. Imported trace content and answers are never stored.

Live retrieval measures elapsed latency. Its embedding adapters currently do
not expose usage counters to this seam, so `accounting_complete` is false;
zero reported cost/tokens must not be interpreted as free inference. Imported
traces have `accounting_complete=true` only when every trace explicitly supplies
all three usage counters; omitted counters do not imply known zero. Faithfulness
defaults to the documented lexical-overlap proxy, not an LLM judge.

PostgreSQL migration `0014_evaluation_storage` creates workspace-scoped
dataset/run tables with cascading workspace deletion. No golden JSONL file is
modified. Tests apply this migration only in an isolated temporary schema.

Saved-run comparisons use `POST /v1/workspaces/{workspace_id}/eval/comparisons`
with `baseline_run_id`, `candidate_run_id` and `dimension`. The runs must share
dataset identity/hash, sample count, scorer, sensitivity and accounting mode;
exactly the selected setting must differ. Live settings are `mode`, `top_k`
and `reranker`. Imported pipeline traces additionally accept an `experiment`
object identifying `embedder`, `chunker`, `retriever`, `model` or `prompt`.
These identifiers describe the external pipeline that produced the traces;
they do not claim to reconfigure live retrieval. All listed experiment
dimensions can be compared with the same one-change validation.
