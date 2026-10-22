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
#   * ANY model pin in those files points at a model not on the ALLOWED list.
#
# A "model pin" is an assignment to a MODEL-named constant. The pin syntax
# varies by file, so ALL of these forms are recognised (optional whitespace
# on both sides of "=", quoted or bare value):
#
#   DEFAULT_MODEL = "BAAI/bge-m3"          # Python adapter (spaced, quoted)
#   DEFAULT_MODEL="BAAI/bge-m3"            # Python adapter (unspaced)
#   DEFAULT_MULTIMODAL_EMBEDDING_MODEL = "google/gemini-embedding-2"
#   EMBEDDING_MODEL=google/gemini-embedding-2   # env file (bare value)
#
# A right-hand side that is not a plain literal (e.g. os.environ.get(...))
# is not a pin: the literal it falls back to is pinned on its own line.
#
# Usage:
#   scripts/model_origin_guard.sh              # scan the repository sources
#   scripts/model_origin_guard.sh --selftest   # run the executable controls
#   scripts/model_origin_guard.sh <file>...    # scan the given files only
# Always exit 0; report failures on stdout and exit 1.

set +e

MODEL_FAILED=0

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

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
    "packages/retrieval/adapters/multimodal_embed.py"
    ".env.example"
)

# A model pin: NAME = "value" (Python, quoted) or NAME=value (env, bare),
# with optional whitespace around "=". NAME is any MODEL-named constant.
PIN_RE="^[[:space:]]*[A-Z][A-Z0-9_]*MODEL[A-Z0-9_]*[[:space:]]*=[[:space:]]*[\"'][^\"']*[\"']|^[A-Z][A-Z0-9_]*MODEL[A-Z0-9_]*=[A-Za-z0-9._:/-]+"

# Print "NAME=VALUE" for every model pin in the given file.
extract_pins() {
    grep -oE "$PIN_RE" "$1" 2>/dev/null \
        | sed -E "s/^[[:space:]]*([A-Z][A-Z0-9_]*MODEL[A-Z0-9_]*)[[:space:]]*=[[:space:]]*[\"']?([^\"']*)[\"']?$/\1=\2/"
}

# Replace the value of every literal model pin with an unlisted probe.
# (Control fixture for the selftest; the production tree is never touched.)
unlist_pins() {
    sed -E "s/^([[:space:]]*[A-Z][A-Z0-9_]*MODEL[A-Z0-9_]*[[:space:]]*=[[:space:]]*)([\"'][^\"']*[\"']|[A-Za-z0-9._:/-]+)/\1\"unlisted\/police-probe\"/" "$1"
}

# Scan one file: China-origin patterns, then EVERY model pin.
scan_file() {
    local src="$1"

    # a) Reject any flagged China-origin pattern in the adapter sources.
    for pat in "${REJECTED[@]}"; do
        if grep -Eq "$pat" "$src"; then
            echo "FAIL: model origin guard - rejected pattern '$pat' in $src"
            MODEL_FAILED=1
        fi
    done

    # b) EVERY model pin must be on the allowlist (each pin is checked, not
    #    just the first one).
    while IFS= read -r pin; do
        [ -n "$pin" ] || continue
        pin_name="${pin%%=*}"
        model="${pin#*=}"
        [ -n "$model" ] || continue
        allowed=0
        for m in "${ALLOWED[@]}"; do
            if [ "$model" = "$m" ]; then
                allowed=1
                break
            fi
        done
        if [ "$allowed" -ne 1 ]; then
            echo "FAIL: model origin guard - model '$model' (pin $pin_name) not in allowlist: ${ALLOWED[*]}"
            MODEL_FAILED=1
        fi
    done < <(extract_pins "$src")
}

# --------------------------------------------------------------------------- #
# Executable controls (offline, no network)
# --------------------------------------------------------------------------- #
#
# For every scanned source: the unmodified file passes; an unlisted FIRST pin
# fails; an unlisted SECOND pin in the same file fails; a China-origin pin
# fails. The controls are the regression for the pin-extraction gap (a spaced
# Python assignment used to escape the scan entirely).

expect_exit() {
    local expected="$1"
    local label="$2"
    shift 2
    "$SCRIPT_PATH" "$@" >/dev/null 2>&1
    local rc=$?
    if [ "$rc" -ne "$expected" ]; then
        echo "FAIL: guard selftest - $label: expected exit $expected, got $rc"
        return 1
    fi
    echo "ok: guard selftest - $label (exit $rc)"
    return 0
}

run_selftest() {
    local failed=0
    local tmp
    tmp=$(mktemp -d) || return 1

    for src in "${ADAPTER_SOURCES[@]}"; do
        if [ ! -f "$src" ]; then
            echo "FAIL: guard selftest - scanned source missing: $src"
            failed=1
            continue
        fi
        base="$(basename "$src")"
        probe_first="$tmp/${base}.unlisted-first"
        probe_second="$tmp/${base}.unlisted-second"
        probe_china="$tmp/${base}.china-origin"

        cp "$src" "$tmp/${base}.baseline"
        unlist_pins "$src" >"$probe_first"
        # A SECOND pin in the same file, in both syntaxes.
        { cat "$src"; printf '\nEMBEDDING_MODEL = "unlisted/police-probe"\n'; } >"$tmp/${base}.second-spaced"
        { cat "$src"; printf '\nEMBEDDING_MODEL=unlisted/police-probe\n'; } >"$probe_second"
        { cat "$src"; printf '\nDEFAULT_MODEL = "moonshot/police-probe"\n'; } >"$probe_china"

        expect_exit 0 "approved pins pass ($src)" "$tmp/${base}.baseline" || failed=1
        expect_exit 1 "unlisted FIRST pin fails ($src)" "$probe_first" || failed=1
        expect_exit 1 "unlisted SECOND pin fails, spaced syntax ($src)" \
            "$tmp/${base}.second-spaced" || failed=1
        expect_exit 1 "unlisted SECOND pin fails, bare syntax ($src)" "$probe_second" || failed=1
        expect_exit 1 "China-origin pin fails ($src)" "$probe_china" || failed=1
    done

    rm -rf "$tmp"
    if [ "$failed" -ne 0 ]; then
        echo "FAIL: model origin guard selftest (PD-005 v3)"
        return 1
    fi
    echo "PASS: model origin guard selftest (PD-005 v3)"
    return 0
}

# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

if [ "${1:-}" = "--selftest" ]; then
    run_selftest
    exit $?
fi

# Positional arguments override the scanned source list (used by the selftest).
if [ "$#" -gt 0 ]; then
    ADAPTER_SOURCES=("$@")
fi

for src in "${ADAPTER_SOURCES[@]}"; do
    [ -f "$src" ] || continue
    scan_file "$src"
done

if [ "$MODEL_FAILED" -ne 0 ]; then
    echo "FAIL: model origin guard (PD-005 v3)"
    exit 1
fi

echo "PASS: model origin guard (PD-005 v3) - no China-origin models"
