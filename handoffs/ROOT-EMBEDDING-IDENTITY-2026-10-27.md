# ROOT handoff — stable embedding encoder identity (2026-10-27)

Date: 2026-10-27
Owner: root (verification + commit lane) / Reviewer: coordinator + review1
Scope: working tree at HEAD before this commit. No DONE marking, no push, no deploy.

## CHANGE

- `packages/models/adapters/openrouter_provider.py` — adapter exposes
  `embedding_identity_provider` ClassVar pinning the stable encoder label
  ("GeminiEmbedding2") across a Python transport-class refactor.
- `packages/retrieval/embedding_identity.py` — `provider_identity()` honours a
  trusted adapter's explicit label (validated: must be a non-empty,
  untrimmed-exact str) and otherwise keeps the class name; all four identity
  fields still match exactly, no comparison alias.
- `scripts/reindex_all_chunks.py` — reindex stamping now calls the shared
  identity helper instead of a parallel dict implementation (public dict shape
  preserved).
- `tests/unit/test_embedding_provider_label.py` (new, 16 tests) — legacy
  `scripts/rebuild_with_manifest.GeminiEmbedding2` and the current adapter send
  byte-identical payloads and record identical identity; malformed labels
  rejected without fallback; strict four-field comparison never weakened.
- `tests/integration/test_embedding_label_payload.py` (new) — reversible nested
  label update on a private real-Qdrant collection (exact-identity selection,
  audited IDs only, vectors/payload preserved, restore verified).

## WHY

`scripts/rebuild_with_manifest.GeminiEmbedding2` persisted the label
"GeminiEmbedding2"; after the transport-class refactor the class name
("OpenRouterProvider") would have made every stored collection's identity
mismatch — a loud failure where none belongs. DECISIONS row: strict identity
comparison (model/revision/dimension mandatory) stays intact; the label pin is
not an alias.

## VERIFY

```
/root/ko-venv/bin/python -m pytest tests/unit -k "embedding or identity" \
  --ignore=tests/unit/test_docling_chunker.py --ignore=tests/unit/test_rerank_sidecar.py -q
# 89 passed
TMPDIR=/var/tmp /root/ko-venv/bin/python -m pytest tests/integration/test_embedding_label_payload.py -q
# 1 passed (real Qdrant fixture collection created + deleted)
/root/ko-venv/bin/python -m ruff check packages/models/adapters/openrouter_provider.py \
  packages/retrieval/embedding_identity.py scripts/reindex_all_chunks.py \
  tests/unit/test_embedding_provider_label.py tests/integration/test_embedding_label_payload.py
# All checks passed
```

## REUSED

No third-party code. httpx MockTransport for the fixture-only payload parity
test; qdrant-client for the reversible-label integration proof.

## RISKS

Identity is a hard gate: a wrongly pinned label would make stored collections
mismatched (loud, fail-closed, never silent). The explicit label is validated
per call; adapters without the attribute keep their existing class identity.
Live production reindex/migration is NOT part of this commit — it stays behind
the existing release gates.

## Regression context

Full unit suite on a pristine HEAD worktree (`fe3c373`) vs this tree: the tree's
failure set is a subset of HEAD's — zero regressions introduced by this change
(see ROOT-TEST-FIXES-2026-10-27.md for the companion evidence).
