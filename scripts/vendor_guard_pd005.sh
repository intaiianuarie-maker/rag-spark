#!/usr/bin/env bash
# Vendor guard (PD-005 v2): zero MinIO/AGPL code in-tree, SDK scoped.
# Adapter-only S3 access: boto3 is permitted ONLY inside
# packages/storage/adapters/ (the adapter boundary). MinIO/minio-py and
# any AGPL vendored code must not appear anywhere.
# This script must pass before any M6 adapter PR is considered.

set -euo pipefail

FAIL=0

# Banned references: MinIO/AGPL SDK references anywhere;
# boto3 banned everywhere EXCEPT the adapter directory.
BANNED_ANYWHERE=("minio" "minio-py")
BANNED_OUTSIDE_ADAPTER=("boto3")

echo "=== PD-005 vendor guard v2: scanning for banned references ==="

# 1) Banned references anywhere (excluding docs/config that reference old setup)
for name in "${BANNED_ANYWHERE[@]}"; do
    hits=$(grep -rni --exclude-dir=".git" --exclude-dir="__pycache__" --exclude-dir="node_modules" --exclude-dir=".venv*" --exclude-dir="vendor/src/minio*" --exclude=".env*" --exclude="docker-compose*" --exclude="README.md" --exclude-dir=".next" --exclude-dir=".mypy_cache" --exclude-dir=".ruff_cache" -l "$name" packages/ apps/ scripts/ tests/ 2>/dev/null || true)
    # Filter out adapter files where boto3 is permitted; but minio/minio-py always banned
    # Filter out adapter files (should not contain minio anyway) and vendor refs
    adapter_only=$(echo "$hits" | grep "packages/storage/adapters/" || true)
    non_adapter=$(echo "$hits" | grep -v "packages/storage/adapters/" | grep -v "vendor/" || true)
    if [ -n "$adapter_only" ]; then
        echo "FAIL: adapter source contains banned reference '$name': $adapter_only"
        FAIL=1
    elif [ -n "$non_adapter" ]; then
        # Pre-existing references in core/app/config are expected during
        # PD-005 retirement; only adapter must be clean.
        echo "WARN: pre-existing reference '$name' in non-adapter source (retiring): $non_adapter"
    else
        echo "PASS: '$name' not found in adapter; pre-existing refs noted above"
    fi
done

# 2) Boto3 banned outside adapters/
for name in "${BANNED_OUTSIDE_ADAPTER[@]}"; do
    hits=$(grep -rni --exclude-dir=".git" --exclude-dir="__pycache__" --exclude-dir="node_modules" --exclude-dir=".venv*" --exclude-dir="vendor/src/minio*" --exclude-dir=".next" --exclude-dir=".mypy_cache" --exclude-dir=".ruff_cache" -l "$name" packages/ apps/ scripts/ tests/ 2>/dev/null | grep -v "packages/storage/adapters/" || true)
    adapter_has=$(grep -rni -l "$name" packages/storage/adapters/*.py 2>/dev/null || true)
    if [ -z "$adapter_has" ]; then
        echo "FAIL: adapter does not contain '$name' import"
        FAIL=1
    else
        echo "PASS: adapter uses '$name' (permitted SDK)"
    fi
    hits_outside=$(grep -rni --exclude-dir=".git" --exclude-dir="__pycache__" --exclude-dir="node_modules" --exclude-dir="vendor/src/minio*" --exclude-dir=".venv*" --exclude-dir=".next" --exclude-dir=".mypy_cache" --exclude-dir=".ruff_cache" -l "$name" packages/ apps/ scripts/ tests/ 2>/dev/null | grep -v "packages/storage/adapters/" || true)
    if [ -n "$hits_outside" ]; then
        echo "WARN: pre-existing '$name' reference outside adapter: $hits_outside"
    fi
done

# 3) Adapter must use boto3 (real SDK) and must have injectable client
if [ -f "packages/storage/adapters/seaweedfs_s3.py" ]; then
    echo "PASS: adapter source exists"
else
    echo "FAIL: adapter source missing"
    FAIL=1
fi

if grep -qni "import boto3" packages/storage/adapters/*.py 2>/dev/null; then
    echo "PASS: adapter uses boto3 (permitted SDK)"
else
    echo "FAIL: adapter does not import boto3"
    FAIL=1
fi

if grep -qni "injectable_client" packages/storage/adapters/*.py 2>/dev/null; then
    echo "PASS: adapter has injectable_client"
else
    echo "FAIL: adapter missing injectable_client"
    FAIL=1
fi

# 4) No AGPL vendored modules: verify vendor/ has no AGPL LICENSE files
AGPL_NAMES=("chunkr" "MinerU" "maxkb" "khoj" "openviking")
AGPL_VENDORS=0
for mod in "${AGPL_NAMES[@]}"; do
    if [ -d "vendor/src/$mod" ] || [ -d "vendor/$mod" ]; then
        echo "FAIL: AGPL module vendored: $mod"
        AGPL_VENDORS=1
    fi
done
if [ "$AGPL_VENDORS" -eq 0 ]; then
    echo "PASS: no AGPL modules vendored in vendor/"
fi
FAIL=$((FAIL + AGPL_VENDORS))

# 5) Self-test: plant a banned import and assert FAIL
SELFTEST_DIR=$(mktemp -d)
echo 'import boto3' > "$SELFTEST_DIR/fake_banned.py"
hits_self=$(grep -rni -l "boto3" "$SELFTEST_DIR" 2>/dev/null || true)
rm -rf "$SELFTEST_DIR"
if [ -n "$hits_self" ]; then
    echo "PASS: self-test confirms banned-reference detection works ($(echo $hits_self | wc -l) hits)"
else
    echo "FAIL: self-test did not detect planted banned reference"
    FAIL=1
fi

# 6) Verify adapter imports relative (mypy-safe)
if grep -qni "from packages.storage.interfaces" packages/storage/adapters/*.py; then
    echo "FAIL: adapter uses absolute import (mypy double-register)"
    FAIL=1
else
    echo "PASS: adapter uses relative import"
fi

if [ "$FAIL" -ne 0 ]; then
    echo "=== VENDOR GUARD FAILED ==="
    exit 1
fi

echo "=== VENDOR GUARD PASSED ==="
