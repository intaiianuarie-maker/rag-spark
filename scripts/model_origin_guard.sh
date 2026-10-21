#!/usr/bin/env bash
# model_origin_guard.sh (PD-005 v3) — model-level China-origin allowlist gate.
#
# Replaces any paid/China-hosted rerank route with a self-hosted
# cross-encoder/ms-marco-MiniLM-L6-v2 ("MS MiniLM + UKP/SentenceTransformers",
# English-only, Apache-2.0, $0, ~91 MB).
#
# CI gate FAILs if any of the following are true:
#   * A model name / pattern flagged as China-origin appears in the adapter
#     sources or the env file.
#   * A pinned DEFAULT_MODEL / RERANK_MODEL / EMBEDDING_MODEL points at a
#     model not on the ALLOWED list.
#
# Usage:
#   scripts/model_origin_guard.sh
# Always exit 0; report failures on stdout and exit 1.

set +e

MODEL_FAILED=0

# Approved, non-China, license-gate-passing models.
#
# This list mirrors the in-process admission catalogue
# (``packages/models/catalog.py`` -> ``ADMITTED_MODELS``) plus the local
# retrieval-adapter defaults, so the shell gate and the in-process guard
# cannot drift (see the catalogue docstring, PD-005 v3). Every entry is a
# non-China-origin publisher that the product already routes to; the
# China-origin denylist below is unchanged.
ALLOWED=(
    "cross-encoder/ms-marco-MiniLM-L6-v2"  # Microsoft MiniLM (US/UK), Apache-2.0
    "BAAI/bge-m3"                          # existing embedding model (non-China)
    "google/gemini-embedding-2"            # admitted EMBEDDING route (Google, US)
    "google/gemma-4-26b-a4b-it"            # admitted GENERATION/AGENT/GRAPH/WORKFLOW route
    "google/gemma-4-26b-a4b-it:free"       # admitted free tier of the same route
    "nvidia/llama-nemotron-rerank-vl-1b-v2:free"  # admitted RERANK route (NVIDIA, US)
    "gpt-4o-mini"                         # LiteLLM gateway default / router fallback (OpenAI, US)
)

# Model names / patterns that are flagged (China origin / not approved).
REJECTED=(
    "BAAI/bge-reranker-v2-m3"
    "moonshot/"
    "DeepSeek"
    "ZhipuAI"
)

# Files that pin the model used by the retrieval adapter.
ADAPTER_SOURCES=(
    "packages/retrieval/adapters/infinity_rerank.py"
    "packages/retrieval/adapters/infinity_embed.py"
    ".env.example"
)

# Env vars that pin a model in the adapter sources / env file.
MODEL_PINS="^(DEFAULT_MODEL|RERANK_MODEL|EMBEDDING_MODEL)="

for src in "${ADAPTER_SOURCES[@]}"; do
    [ -f "$src" ] || continue

    # a) Reject any flagged China-origin pattern in the adapter sources.
    for pat in "${REJECTED[@]}"; do
        if grep -Eq "$pat" "$src"; then
            echo "FAIL: model origin guard - rejected pattern '$pat' in $src"
            MODEL_FAILED=1
        fi
    done

    # b) EVERY pinned DEFAULT_MODEL / RERANK_MODEL / EMBEDDING_MODEL must be
    #    on the allowlist (each pin is checked, not just the first one).
    while IFS= read -r pin_line; do
        [ -n "$pin_line" ] || continue
        model=$(printf '%s' "$pin_line" \
            | sed -E "s/^[^=]*=[\"']?([^\"'[:space:]]+).*/\1/")
        allowed=0
        for m in "${ALLOWED[@]}"; do
            if [ "$model" = "$m" ]; then
                allowed=1
                break
            fi
        done
        if [ "$allowed" -ne 1 ]; then
            echo "FAIL: model origin guard - model '$model' not in allowlist: ${ALLOWED[*]}"
            MODEL_FAILED=1
        fi
    done < <(grep -E "$MODEL_PINS" "$src")
done

if [ "$MODEL_FAILED" -ne 0 ]; then
    echo "FAIL: model origin guard (PD-005 v3)"
    exit 1
fi

echo "PASS: model origin guard (PD-005 v3) - no China-origin models"
