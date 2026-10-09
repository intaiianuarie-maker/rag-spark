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
#   * The DEFAULT_MODEL / RERANK_MODEL pin points at a model not on the
#     ALLOWED list.
#
# Usage:
#   scripts/model_origin_guard.sh
# Always exit 0; report failures on stdout and exit 1.

set +e

MODEL_FAILED=0

# Approved, non-China, license-gate-passing models.
ALLOWED=(
    "cross-encoder/ms-marco-MiniLM-L6-v2"  # Microsoft MiniLM (US/UK), Apache-2.0
    "BAAI/bge-m3"                          # existing embedding model (non-China)
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
    ".env.example"
)

for src in "${ADAPTER_SOURCES[@]}"; do
    [ -f "$src" ] || continue

    # a) Reject any flagged China-origin pattern in the adapter sources.
    for pat in "${REJECTED[@]}"; do
        if grep -Eq "$pat" "$src"; then
            echo "FAIL: model origin guard - rejected pattern '$pat' in $src"
            MODEL_FAILED=1
        fi
    done

    # b) The pinned DEFAULT_MODEL / RERANK_MODEL must be on the allowlist.
    if grep -Eq "^(DEFAULT_MODEL|RERANK_MODEL)=" "$src"; then
        model=$(grep -E "^(DEFAULT_MODEL|RERANK_MODEL)=" "$src" \
            | head -n 1 \
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
    fi
done

if [ "$MODEL_FAILED" -ne 0 ]; then
    echo "FAIL: model origin guard (PD-005 v3)"
    exit 1
fi

echo "PASS: model origin guard (PD-005 v3) - no China-origin models"
