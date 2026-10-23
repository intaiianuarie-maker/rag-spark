#!/usr/bin/env bash
# model_origin_guard.sh (PD-005 v4) — model-level China-origin allowlist gate.
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
#   * dotenv sources (`.env`, `.env.*`) are parsed with `python-dotenv` when it
#     is importable — the exact runtime semantics: an optional `export`
#     keyword, optional whitespace (incl. TAB) around `=`, an optional
#     surrounding pair of quotes, and an inline `#` comment that starts after
#     ANY whitespace (space or tab) but never inside a quoted value. When
#     `python-dotenv` is absent, a manual parser reproduces those rules.
#
# The COMPLETE literal value is validated — never a prefix of it — and the
# validation happens IN-PROCESS, before any value crosses the shell boundary
# (so a value containing a newline / NUL / tab can never be split into a
# forged second record or truncated to an approved prefix). An empty,
# whitespace-only or unapproved value FAILS the gate (it is not skipped). So
# `NAME = "allowed" "-suffix"`, `NAME = allowed+suffix`, `NAME = ""` and
# `EMBEDDING_MODEL=` are all rejected.
#
# Failures are NOT swallowed: a file that cannot be read, cannot be parsed, or
# whose pin parser/interpreter fails makes the OUTER gate fail closed (exit 1).
#
# Usage:
#   scripts/model_origin_guard.sh              # scan the repository sources
#   scripts/model_origin_guard.sh --selftest   # run the executable controls
#   scripts/model_origin_guard.sh <file>...    # scan the given files only
# Exits 0 on pass, 1 on failure.

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

# Newline-separated allowlist passed to the in-process pin validator (so it can
# validate the COMPLETE value before anything reaches the shell).
ALLOWED_STR="$(printf '%s\n' "${ALLOWED[@]}")"

# --------------------------------------------------------------------------- #
# In-process pin validator.
#
# Reads the value of every MODEL-named assignment and validates the COMPLETE
# literal against the allowlist (no shell transfer of values). Prints "FAIL:"
# lines on violations and exits:
#   0 = all pins approved
#   1 = a pin value (or empty/unapproved) is not on the allowlist
#   2 = the source cannot be read or parsed (fail closed)
# --------------------------------------------------------------------------- #
verify_pins() {
    GUARD_ALLOWED="$ALLOWED_STR" python3 - "$1" <<'PY'
import ast
import io
import os
import re
import sys
from dotenv.parser import parse_stream

path = sys.argv[1]
allowed = set(os.environ.get("GUARD_ALLOWED", "").splitlines())


def is_model_name(name: str) -> bool:
    return "MODEL" in name and name == name.upper() and bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", name))


def python_value(node):
    try:
        literal = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError) as exc:
        print(f"FAIL: model origin guard - cannot eval pin value with ast.literal_eval: {exc}", file=sys.stderr)
        sys.exit(1)
    if isinstance(literal, bytes):
        literal = literal.decode("utf-8", "replace")
    if not isinstance(literal, str):
        literal = str(literal)
    return literal


def python_pins(source: str):
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
            if isinstance(target, ast.Name) and is_model_name(target.id):
                literal = python_value(value)
                if literal is None:
                    continue
                pins.append((target.id, literal))
    return pins


def dotenv_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    # Quoted value: the content of the matching quotes; a '#' after the closing
    # quote (preceded by whitespace) is a comment.
    if value[0] in "\"'" and len(value) >= 2:
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            inner = value[1:end]
            tail = value[end + 1:].lstrip()
            if not tail or tail.startswith("#"):
                return inner
            return value
    # Unquoted: a '#' preceded by ANY whitespace starts a comment.
    match = re.search(r"\s#", value)
    if match:
        value = value[: match.start()].rstrip()
    return value


def dotenv_pins_manual(source: str):
    # Split on the runtime's line terminators only (\r\n, \r, \n). Embedded
    # VT / FF / U+2028 are NOT line breaks here, so they cannot truncate a
    # value to an approved prefix or forge a second record.
    pins = []
    for raw in source.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export") and len(line) > 6 and line[6].isspace():
            line = line[6:].strip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().split(":", 1)[0].strip()
        if not is_model_name(name):
            continue
        pins.append((name, dotenv_value(value)))
    return pins


def dotenv_pins(source: str):
    # Use dotenv.parser.parse_stream to validate EVERY model pin without
    # collapsing duplicates (dotenv_values returns a dict and keeps only the
    # last value) or ignoring malformed bindings. The parse_stream iterator
    # yields Binding namedtuples with (key, value, original, error).
    # CRITICAL: Check binding.error FIRST, before skipping key=None. This
    # ensures malformed bindings (e.g. unterminated quotes) fail the gate
    # closed instead of being silently swallowed.
    pins = []
    for binding in parse_stream(io.StringIO(source)):
        # If there's a parse error (e.g. unterminated quote, None key), fail
        # closed immediately. Must check error BEFORE checking key is None.
        if binding.error:
            print(f"FAIL: model origin guard - malformed dotenv binding at line {binding.index}: {binding.original}")
            sys.exit(1)
        # Skip comment lines where key is None (no error, just a comment).
        if binding.key is None:
            continue
        # Only process MODEL-named pins.
        if not is_model_name(binding.key):
            continue
        value = binding.value if binding.value is not None else ""
        if value not in allowed:
            print(f"FAIL: model origin guard - model {value!r} (pin {binding.key}) not in allowlist: {', '.join(sorted(allowed))}")
            sys.exit(1)
        pins.append((binding.key, value))
    return pins


try:
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
except OSError as exc:
    print(f"FAIL: model origin guard - cannot read {path}: {exc}")
    sys.exit(2)

try:
    if path.endswith(".py"):
        pins = python_pins(source)
    else:
        pins = dotenv_pins(source)
except SyntaxError as exc:
    print(f"FAIL: model origin guard - cannot parse {path}: {exc}")
    sys.exit(2)
except Exception as exc:
    print(f"FAIL: model origin guard - cannot parse {path}: {exc}")
    sys.exit(2)

# For .py files: validate each pin against the allowlist.
# For .env files: all pins have already been validated inside dotenv_pins;
# fall through to success.
if path.endswith(".py"):
    failed = False
    for name, value in pins:
        if value not in allowed:
            print(f"FAIL: model origin guard - model {value!r} (pin {name}) not in allowlist: {', '.join(sorted(allowed))}")
            failed = True
    sys.exit(1 if failed else 0)
PY
}

# Scan one file: China-origin patterns, then EVERY model pin (validated
# in-process). Any read/parse/interpreter failure fails the gate closed.
scan_file() {
    local src="$1"

    # a) Reject any flagged China-origin pattern (byte-identical REJECTED).
    for pat in "${REJECTED[@]}"; do
        grep -Eq "$pat" "$src"
        local grc=$?
        if [ "$grc" -eq 0 ]; then
            echo "FAIL: model origin guard - rejected pattern '$pat' in $src"
            MODEL_FAILED=1
        elif [ "$grc" -ge 2 ]; then
            echo "FAIL: model origin guard - cannot read $src (pattern scan: '$pat')"
            MODEL_FAILED=1
        fi
    done

    # b) Every model pin (complete literal value) is validated in-process by
    #    the Python helper. The helper's exit code is propagated: a nonzero
    #    exit (parse/read/interpreter error or a bad pin) fails the gate.
    local verdict jrc
    verdict=$(verify_pins "$src")
    jrc=$?
    [ -n "$verdict" ] && printf '%s\n' "$verdict"
    if [ "$jrc" -ne 0 ]; then
        if [ -z "$verdict" ]; then
            echo "FAIL: model origin guard - pin check failed for $src (parser/interpreter error)"
        fi
        MODEL_FAILED=1
    fi
}

# --------------------------------------------------------------------------- #
# Executable controls (offline, no network)
# --------------------------------------------------------------------------- #
#
# 1. For every scanned source: the unmodified file passes; an unlisted FIRST
#    pin fails; an unlisted SECOND pin fails (spaced + bare syntax); a
#    China-origin pin fails.
# 2. A syntax matrix: quoted/bare values, whitespace variants, the `export`
#    keyword, annotated assignments, raw/parenthesised/triple-quoted literals,
#    adjacent literals, allowed-prefix + unlisted suffix, `#`-suffix, and
#    empty/whitespace-only values — approved controls pass, everything else
#    fails.
# 3. Byte-level controls: a value carrying a real control char (newline / NUL /
#    tab / embedded VT / FF / U+2028) is validated COMPLETELY in-process and
#    never truncated to an approved prefix or split into a forged record.
# 4. Failure-injection: a malformed source, a missing/unreadable source, and a
#    failing interpreter each fail the OUTER gate (no swallowing).
# 5. dotenv parity: the guard's whitespace/comment/tab rules match
#    python-dotenv (see the version line printed by `--selftest`).

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
    'export keyword with tab|1||export	EMBEDDING_MODEL=unlisted/police-probe'
    'env allowed prefix plus suffix|1||EMBEDDING_MODEL=google/gemini-embedding-2+unlisted'
    'env hash suffix|1||EMBEDDING_MODEL=google/gemini-embedding-2#unlisted'
    'raw python literal|1|.py|DEFAULT_MODEL = r"unlisted/police-probe"'
    'parenthesised python literal|1|.py|DEFAULT_MODEL = ("unlisted/police-probe")'
    'adjacent python literals|1|.py|DEFAULT_MODEL = "gpt-4o-mini" "-unlisted"'
    'adjacent python literals approved (one value)|0|.py|DEFAULT_MODEL = "gpt-4o-" "mini"'
    'triple quoted python literal|1|.py|DEFAULT_MODEL = """unlisted/police-probe"""'
    'rejected vendor|1||DEFAULT_MODEL = "moonshot/police-probe"'
)

run_selftest() {
    local failed=0
    local tmp
    tmp=$(mktemp -d) || return 1

    # Note the python-dotenv version (parity target) when available.
    local dv
    dv=$(python3 - <<'PY' 2>/dev/null
try:
    from importlib.metadata import version
    import dotenv
    print("python-dotenv " + version("python-dotenv"))
except Exception:
    print("python-dotenv not importable (manual dotenv parser, parity verified on controls)")
PY
)
    echo "parity: $dv"

    for src in "${ADAPTER_SOURCES[@]}"; do
        if [ ! -f "$src" ]; then
            echo "FAIL: guard selftest - scanned source missing: $src"
            failed=1
            continue
        fi
        local base
        base="$(basename "$src")"
        local probe_first="$tmp/${base}.unlisted-first"
        local probe_second="$tmp/${base}.unlisted-second"
        local probe_china="$tmp/${base}.china-origin"

        cp "$src" "$tmp/$(basename "$src")"
        # Replace the value of every literal model pin with an unlisted probe.
        sed -E "s/^([[:space:]]*[A-Z][A-Z0-9_]*MODEL[A-Z0-9_]*[[:space:]]*=[[:space:]]*)([\"'][^\"']*[\"']|[A-Za-z0-9._:/-]+)/\1\"unlisted\/police-probe\"/" "$src" >"$probe_first"
        # A SECOND pin in the same file, in both syntaxes.
        { cat "$src"; printf '\nEMBEDDING_MODEL = "unlisted/police-probe"\n'; } >"$tmp/${base}.second-spaced"
        { cat "$src"; printf '\nEMBEDDING_MODEL=unlisted/police-probe\n'; } >"$probe_second"
        { cat "$src"; printf '\nDEFAULT_MODEL = "moonshot/police-probe"\n'; } >"$probe_china"

        expect_exit 0 "approved pins pass ($src)" "$tmp/$(basename "$src")" || failed=1
        expect_exit 1 "unlisted FIRST pin fails ($src)" "$probe_first" || failed=1
        expect_exit 1 "unlisted SECOND pin fails, spaced syntax ($src)" \
            "$tmp/${base}.second-spaced" || failed=1
        expect_exit 1 "unlisted SECOND pin fails, bare syntax ($src)" "$probe_second" || failed=1
        expect_exit 1 "China-origin pin fails ($src)" "$probe_china" || failed=1
    done

    # The syntax matrix (the extension decides python vs dotenv parsing).
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

    # Byte-level controls: the COMPLETE value (incl. control chars) is
    # validated in-process; a newline / NUL cannot truncate to an approved
    # prefix or forge a second allow-listed record.
    printf 'DEFAULT_MODEL = "gpt-4o-mini\\n"\n' >"$tmp/byte-newline.py"
    printf 'DEFAULT_MODEL = "gpt-4o-mini\\x00"\n' >"$tmp/byte-nul.py"
    printf 'DEFAULT_MODEL = "gpt-4o-mini\\nEMBEDDING_MODEL=gpt-4o-mini"\n' >"$tmp/byte-forgery.py"
    printf 'DEFAULT_MODEL = "gpt-4o-mini\\t"\n' >"$tmp/byte-tab.py"
    expect_exit 1 "byte: trailing newline in value rejected" "$tmp/byte-newline.py" || failed=1
    expect_exit 1 "byte: NUL in value rejected" "$tmp/byte-nul.py" || failed=1
    expect_exit 1 "byte: record forgery by newline rejected" "$tmp/byte-forgery.py" || failed=1
    expect_exit 1 "byte: trailing tab in value rejected" "$tmp/byte-tab.py" || failed=1

    # dotenv parity controls (match python-dotenv's whitespace/comment rules).
    printf 'export\tEMBEDDING_MODEL=unlisted/police-probe\n' >"$tmp/env-export-tab.env"
    printf 'EMBEDDING_MODEL=google/gemini-embedding-2\t# legitimate comment\n' >"$tmp/env-tab-comment.env"
    printf 'EMBEDDING_MODEL=google/gemini-embedding-2\013unlisted\n' >"$tmp/env-vt.env"
    printf 'EMBEDDING_MODEL=google/gemini-embedding-2\014unlisted\n' >"$tmp/env-ff.env"
    printf 'EMBEDDING_MODEL=google/gemini-embedding-2\u2028unlisted\n' >"$tmp/env-u2028.env"
    expect_exit 1 "parity: export keyword with a TAB is not skipped" "$tmp/env-export-tab.env" || failed=1
    expect_exit 0 "parity: TAB before # is a comment (approved value survives)" "$tmp/env-tab-comment.env" || failed=1
    expect_exit 1 "parity: embedded VT does not forge an approved prefix" "$tmp/env-vt.env" || failed=1
    expect_exit 1 "parity: embedded FF does not forge an approved prefix" "$tmp/env-ff.env" || failed=1
    expect_exit 1 "parity: embedded U+2028 does not forge an approved prefix" "$tmp/env-u2028.env" || failed=1

    # Failure-injection: parse/read/interpreter failures propagate to the OUTER
    # gate (no swallowing via process substitution or command substitution).
    printf 'DEFAULT_MODEL = "unlisted/police-probe"\n(\n' >"$tmp/malformed.py"
    expect_exit 1 "injection: malformed python fails the outer gate" "$tmp/malformed.py" || failed=1
    expect_exit 1 "injection: missing source fails the outer gate" "$tmp/does-not-exist" || failed=1

    # Stub interpreter: python3 -> /bin/false must fail the gate closed.
    # Only python3 is shadowed; the rest of PATH (bash, grep, ...) is kept real
    # so the shebang and the pattern scan still resolve.
    local stubbin
    stubbin=$(mktemp -d)
    ln -sf /usr/bin/false "$stubbin/python3"
    printf 'DEFAULT_MODEL = "gpt-4o-mini"\n' >"$tmp/stub-approved.py"
    ( PATH="$stubbin:$PATH" "$SCRIPT_PATH" "$tmp/stub-approved.py" ) >/dev/null 2>&1
    local stubrc=$?
    if [ "$stubrc" -ne 1 ]; then
        echo "FAIL: guard selftest - injection: stub python3 did not fail the gate (expected 1, got $stubrc)"
        failed=1
    else
        echo "ok: guard selftest - injection: stub python3 fails the gate (exit 1)"
    fi
    rm -rf "$stubbin"

    rm -rf "$tmp"
    if [ "$failed" -ne 0 ]; then
        echo "FAIL: model origin guard selftest (PD-005 v4)"
        return 1
    fi
    echo "PASS: model origin guard selftest (PD-005 v4)"
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
# A source that is given explicitly but missing/unreadable fails the gate.
if [ "$#" -gt 0 ]; then
    ADAPTER_SOURCES=("$@")
fi

for src in "${ADAPTER_SOURCES[@]}"; do
    if [ ! -f "$src" ]; then
        echo "FAIL: model origin guard - source missing: $src"
        MODEL_FAILED=1
        continue
    fi
    scan_file "$src"
done

if [ "$MODEL_FAILED" -ne 0 ]; then
    echo "FAIL: model origin guard (PD-005 v4)"
    exit 1
fi

echo "PASS: model origin guard (PD-005 v4) - no China-origin models"
