# Per-answer diagnostics

JSON `/ask` and the terminal verified SSE `/stream` frame include optional
`debug_trace`. The engine records each call independently; it does not mutate
the shared retriever/provider. The trace contains no prompt body, generated
answer, query text or retrieved source text. Existing authorization and
citation checks still apply.

Version 1 fields:

- `query`: SHA256 of the submitted query and character count.
- `rewrite.status`: `not_reported`; no rewritten query is invented.
- `filters`: actual `workspace_ids`, `include_public`, and retrieval `mode`.
- `retrieval`: observed `status`, measured `latency_ms`,
  `candidate_scope="returned_context_only"`, and `items`. Each item contains
  `chunk_id`, `document_id`, `workspace_id`, finite final `score` and the
  observed dense/lexical/fusion/rerank `scores` dictionary. Missing scoring
  legs remain missing; these are returned context candidates, not a fabricated
  complete ranking of the corpus.
- `context`: `chunk_ids` and `source_text_included=false`.
- `generation`: `status`, actual `model`, `prompt_template_sha256` (the
  system-instruction template version), measured `latency_ms`,
  `prompt_tokens`, `completion_tokens`, `usage_status`. Streaming token usage
  is `null`/`not_reported` because the provider streaming interface emits text
  only. Zero is never substituted for unknown usage.
- `verification`: `phase="parse_verify_publish"`, resulting publication or
  answer `status`, measured post-generation `latency_ms` when generation ran,
  and `usage_status="not_reported"` for verification-provider calls.
- `total_latency_ms`: elapsed engine call time through finalization.

Trace data appears only with the final answer; partial token events remain
text-only. Closing the stream closes the underlying provider iterator.
Context membership follows the existing `ACLScope.allows_workspace` contract,
including public-scope calls. UI source-text opt-in must use the existing
authorized passage interface; the diagnostic trace itself never carries it.
