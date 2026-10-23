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
# varies by source, so the extraction is SOURCE-AWARE:
#
#   * Python sources (`.py`) are parsed with the `ast` module and the value is
#     read with `ast.literal_eval` — the same semantics the runtime uses. That
#     covers annotations (`DEFAULT_MODEL: str = "..."`), raw strings
#     (`r"..."`), triple-quoted strings, parenthesised values and ADJACENT
#     string literals, which are CONCATENATED into one value (Python
#     semantics). A right-hand side that is not a literal (e.g.
#     `os.environ.get(...)`) is not a pin; the literal it falls back to is
#     pinned on its own line.
#   * dotenv sources (`.env`, `.env.*`) are read with dotenv semantics:
#     an optional `export` keyword, optional whitespace around `=`, an
#     optional surrounding pair of quotes, and an inline comment that only
#     starts at a `#` preceded by whitespace.
#   * a file of unknown type is read with BOTH interpretations and EVERY pin
#     found by either one is validated.
#
# The COMPLETE literal value is validated — never a prefix of it — and an
# empty, whitespace-only or unapproved value FAILS the gate (it is not
# skipped). So `NAME="allowed" "-suffix"`, `NAME = allowed+suffix` and
# `NAME=""` are all rejected.
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

# The pin extractor is source-aware; it needs a Python parser. Without one the
# gate cannot do its job, so it fails closed instead of passing silently.
if ! command -v python3 >/dev/null 2>&1; then
    echo "FAIL: model origin guard - python3 is required to read model pins"
    exit 1
fi

# Print "NAME=VALUE" for every model pin in the given file.
extract_pins() {
    python3 - "$1" <<'PY'
import ast
import os
import sys

path = sys.argv[1]


def is_model_name(name: str) -> bool:
    """A MODEL-named constant (upper-case, contains MODEL)."""
    return "MODEL" in name and name == name.upper()


def python_pins(source: str):
    """Complete literal values of MODEL-named assignments (Python semantics)."""
    tree = ast.parse(source, filename=path)
    pins = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name) or not is_model_name(target.id):
                continue
            try:
                literal = ast.literal_eval(value)
            except (ValueError, SyntaxError, TypeError):
                # Not a literal (e.g. os.environ.get(...)): not a pinned value.
                continue
            if isinstance(literal, bytes):
                literal = literal.decode("utf-8", "replace")
            if not isinstance(literal, str):
                literal = str(literal)
            pins.append((target.id, literal))
    return pins


def dotenv_value(raw: str) -> str:
    """The dotenv value of an assignment's right-hand side."""
    value = raw.strip()
    if not value:
        return ""
    if value[0] in "\"'":
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            inner = value[1:end]
            rest = value[end + 1 :].strip()
            if rest == "" or rest.startswith("#"):
                return inner
            # Trailing junk after the quoted value: keep the whole text, it is
            # not a plain model id.
            return value
    # Unquoted: an inline comment only starts at a "#" preceded by space.
    index = value.find(" #")
    if index != -1:
        value = value[:index].rstrip()
    return value


def dotenv_pins(source: str):
    """dotenv values of MODEL-named assignments."""
    pins = []
    for raw in source.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        # A Python annotation is not part of a dotenv name.
        name = name.split(":", 1)[0].strip()
        if not is_model_name(name):
            continue
        pins.append((name, dotenv_value(value)))
    return pins


with open(path, encoding="utf-8") as handle:
    source = handle.read()

base = os.path.basename(path)
if path.endswith(".py"):
    modes = ["python"]
elif base == ".env" or base.startswith(".env."):
    modes = ["dotenv"]
else:
    # Unknown type: read it both ways and validate every pin either way finds.
    modes = ["python", "dotenv"]

pins = []
for mode in modes:
    try:
        if mode == "python":
            pins.extend(python_pins(source))
        else:
            pins.extend(dotenv_pins(source))
    except SyntaxError as exc:
        if mode == "python" and modes == ["python"]:
            print(f"FAIL: model origin guard - cannot parse {path}: {exc}", file=sys.stderr)
            sys.exit(2)
        # A file that is not valid Python is read as dotenv only.

for name, value in pins:
    print(f"{name}={value}")
PY
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
    #    just the first one). An empty, whitespace-only or unapproved value
    #    FAILS: it is never skipped and never truncated to a prefix.
    while IFS= read -r pin; do
        [ -n "$pin" ] || continue
        pin_name="${pin%%=*}"
        model="${pin#*=}"
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
# 1. For every scanned source: the unmodified file passes; an unlisted FIRST
#    pin fails; an unlisted SECOND pin fails; a China-origin pin fails.
# 2. A syntax matrix: quoted/bare values, whitespace variants, the `export`
#    keyword, annotated assignments, adjacent literals, allowed-prefix +
#    unlisted suffix, and empty/whitespace-only values — approved controls
#    pass, everything else fails.
#
# The controls are the regression for the pin-extraction gap: a spaced
# Python assignment, an annotated assignment, an adjacent literal or an
# empty value used to escape the scan entirely.

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

# name|expected exit|source extension|file content
SELFTEST_MATRIX=(
    'approved quoted|0||DEFAULT_MODEL = "gpt-4o-mini"'
    'unknown quoted|1||DEFAULT_MODEL = "unlisted/police-probe"'
    'unknown second quoted pin|1||DEFAULT_MODEL = "gpt-4o-mini"
EMBEDDING_MODEL = "unlisted/police-probe"'
    'approved bare|0||EMBEDDING_MODEL=google/gemini-embedding-2'
    'unknown bare|1||EMBEDDING_MODEL=unlisted/police-probe'
    'unknown bare spaced|1||EMBEDDING_MODEL = unlisted/police-probe'
    'unknown bare space after equals|1||EMBEDDING_MODEL= unlisted/police-probe'
    'unknown bare space before equals|1||EMBEDDING_MODEL =unlisted/police-probe'
    'unknown second bare spaced pin|1||DEFAULT_MODEL=gpt-4o-mini
EMBEDDING_MODEL = unlisted/police-probe'
    'approved bare spaced|0||EMBEDDING_MODEL = google/gemini-embedding-2'
    'empty python literal|1|.py|DEFAULT_MODEL = ""'
    'empty env literal|1||EMBEDDING_MODEL=""'
    'empty bare env|1||EMBEDDING_MODEL='
    'whitespace only value|1||EMBEDDING_MODEL="   "'
    'annotated python unknown|1|.py|DEFAULT_MODEL: str = "unlisted/police-probe"'
    'annotated python approved|0|.py|DEFAULT_MODEL: str = "gpt-4o-mini"'
    'exported env unknown|1||export EMBEDDING_MODEL=unlisted/police-probe'
    'env allowed prefix plus suffix|1||EMBEDDING_MODEL=google/gemini-embedding-2+unlisted'
    'env hash suffix|1||EMBEDDING_MODEL=google/gemini-embedding-2#unlisted'
    'raw python literal|1|.py|DEFAULT_MODEL = r"unlisted/police-probe"'
    'parenthesised python literal|1|.py|DEFAULT_MODEL = ("unlisted/police-probe")'
    'adjacent python literals|1|.py|DEFAULT_MODEL = "gpt-4o-mini" "-unlisted"'
    'adjacent python literals approved (one value)|0|.py|DEFAULT_MODEL = "gpt-4o-" "mini"'
    'adjacent literals in an unknown-type source are rejected|1||DEFAULT_MODEL = "gpt-4o-" "mini"'
    'triple quoted python literal|1|.py|DEFAULT_MODEL = """unlisted/police-probe"""'
    'rejected vendor|1||DEFAULT_MODEL = "moonshot/police-probe"'
)

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
        # Replace the value of every literal model pin with an unlisted probe.
        sed -E "s/^([[:space:]]*[A-Z][A-Z0-9_]*MODEL[A-Z0-9_]*[[:space:]]*=[[:space:]]*)([\"'][^\"']*[\"']|[A-Za-z0-9._:/-]+)/\1\"unlisted\/police-probe\"/" "$src" >"$probe_first"
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

    # The syntax matrix (each case is a standalone file; the extension
    # decides whether it is read as a Python source or as a dotenv source).
    local index=0
    local entry label expected extension content
    for entry in "${SELFTEST_MATRIX[@]}"; do
        index=$((index + 1))
        label="${entry%%|*}"
        entry="${entry#*|}"
        expected="${entry%%|*}"
        entry="${entry#*|}"
        extension="${entry%%|*}"
        content="${entry#*|}"
        printf '%s\n' "$content" >"$tmp/matrix-$index$extension"
        expect_exit "$expected" "syntax matrix: $label" "$tmp/matrix-$index$extension" || failed=1
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
