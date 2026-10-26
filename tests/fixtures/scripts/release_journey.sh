#!/usr/bin/env bash
# Opt-in live release gate. The browser journey owns every assertion and cleanup.
set -euo pipefail
[ "${RUN_RELEASE_E2E:-}" = "1" ] || { echo "RUN_RELEASE_E2E=1 required"; exit 2; }
RELEASE_SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec node "$RELEASE_SCRIPT_DIR/release_journey_browser.mjs"
