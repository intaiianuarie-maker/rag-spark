#!/usr/bin/env bash
# Proxy-branch matrix for apps/web/src/app/api/health/route.ts (test1, landed fixture).
#
# Provenance: /root/.tmp/m004_proxy_branches.sh (handoffs/M0-003-frontend-test1.md,
# 5/5 PASS). TASK-M0-004 asked for the ad-hoc matrix to become a committed
# fixture, so the script is now self-contained: it starts the three stub
# backends itself, runs an isolated `next start` per scenario (ports 3111-3115)
# with a per-instance API_BASE_URL, and never touches the compose stack
# (web:3001 / api:8000).
#
# Requirements: a production build (apps/web/.next), node/npm, curl, and the
# backend on :8000 for the healthy-passthrough case.
#   cd apps/web && npm run build
#   bash tests/fixtures/scripts/m004_proxy_branches.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
WEB_DIR="$REPO_ROOT/apps/web"
PYTHON_BIN="${PYTHON_BIN:-python3}"
API_BASE="http://127.0.0.1:${API_PORT:-8000}"

export TMPDIR="${TMPDIR:-/root/.tmp}"
mkdir -p "$TMPDIR"
LOG_DIR="${PROXY_MATRIX_LOG_DIR:-$TMPDIR}"

FAIL=0
STUBS_PID=""
SERVER_PID=""

stop_server() {
  if [ -n "$SERVER_PID" ]; then
    # Each `next start` gets its own session so the whole tree (npx -> next-server)
    # dies with it and ports 3111-3115 are released between cases.
    kill -TERM -- "-$SERVER_PID" 2>/dev/null || kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
    SERVER_PID=""
  fi
}

cleanup() {
  stop_server
  if [ -n "$STUBS_PID" ]; then kill "$STUBS_PID" 2>/dev/null; fi
}
trap cleanup EXIT

if [ ! -f "$WEB_DIR/.next/BUILD_ID" ]; then
  echo "PROXY_BRANCHES_FAILED: no production build at $WEB_DIR/.next"
  echo "  fix: cd apps/web && npm run build"
  exit 1
fi

if ! curl -s -o /dev/null --max-time 3 "$API_BASE/health"; then
  echo "PROXY_BRANCHES_FAILED: backend not reachable at $API_BASE (case A needs it)"
  echo "  fix: docker compose up -d   # compose api publishes :8000"
  exit 1
fi

# --- stub backends (503 JSON / 200 non-JSON / 5s slow) ------------------------------
"$PYTHON_BIN" "$SCRIPT_DIR/m004_stubs.py" >"$LOG_DIR/proxy_stubs.log" 2>&1 &
STUBS_PID=$!
for port in 5301 5302 5303; do
  up=0
  for _ in $(seq 1 40); do
    # TCP connect only: the :5303 stub sleeps 5s before answering, so an HTTP
    # readiness probe would always time out and look like a dead server.
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then up=1; break; fi
    sleep 0.25
  done
  if [ "$up" != 1 ]; then
    echo "PROXY_BRANCHES_FAILED: stub on :$port did not start (see $LOG_DIR/proxy_stubs.log)"
    exit 1
  fi
done

run_case() {
  local name="$1" base="$2" want_status="$3" want_pattern="$4" timeout_s="${5:-12}"
  local port="$6" max_elapsed="${7:-0}"
  setsid env API_BASE_URL="$base" npx next start -p "$port" >"$LOG_DIR/next_$port.log" 2>&1 &
  SERVER_PID=$!
  local up=0
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$port/"; then up=1; break; fi
    sleep 0.5
  done
  if [ "$up" != 1 ]; then
    echo "$name: SERVER_FAILED (see $LOG_DIR/next_$port.log)"
    FAIL=1
    stop_server
    return
  fi
  local t0 t1 body status elapsed
  t0=$(date +%s.%N)
  body=$(curl -s --max-time "$timeout_s" -w "|%{http_code}" "http://127.0.0.1:$port/api/health")
  t1=$(date +%s.%N)
  status="${body##*|}"
  body="${body%|*}"
  elapsed=$(awk "BEGIN{printf \"%.2f\", $t1 - $t0}")
  stop_server
  local ok=1
  [ "$status" = "$want_status" ] || ok=0
  echo "$body" | grep -q "$want_pattern" || ok=0
  if [ "$max_elapsed" != "0" ]; then
    if ! awk "BEGIN{exit !($elapsed <= $max_elapsed)}"; then
      echo "      elapsed ${elapsed}s exceeds bound ${max_elapsed}s"
      ok=0
    fi
  fi
  if [ "$ok" = 1 ]; then
    echo "PASS  $name  status=$status elapsed=${elapsed}s"
  else
    echo "FAIL  $name  status=$status (want $want_status) elapsed=${elapsed}s"
    echo "      body=$body"
    echo "      want-pattern=$want_pattern"
    FAIL=1
  fi
}

# name | API_BASE_URL | want HTTP | want body pattern | timeout | port | max elapsed
cd "$WEB_DIR" || { echo "PROXY_BRANCHES_FAILED: cannot cd $WEB_DIR"; exit 1; }
run_case "A_healthy_passthrough"  "$API_BASE"                    "200" '"ok":true'          12 3111
run_case "B_backend_down_502"     "http://127.0.0.1:59999"       "502" 'backend_unreachable' 12 3112
run_case "C_non2xx_passthrough"   "http://127.0.0.1:5301"        "503" 'backend_unhealthy'   12 3113
run_case "D_non_json_502"         "http://127.0.0.1:5302"        "502" 'unexpected_response' 12 3114
run_case "E_timeout_502"          "http://127.0.0.1:5303"        "502" 'timed out'           12 3115 4.5

if [ "$FAIL" = 0 ]; then
  echo "PROXY_BRANCHES_OK  (5/5)"
else
  echo "PROXY_BRANCHES_FAILED"
fi
exit "$FAIL"
