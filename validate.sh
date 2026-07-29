#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# validate.sh — the repository's CANONICAL validation entrypoint.
#
# One command, every production validation intended before promotion:
#   1. environment preflight (fails loudly on missing requirements)
#   2. backend test suite      (pytest, per backend/pytest.ini: -n 2 loadscope)
#   3. API contract tests      (starts a local uvicorn, runs, always stops it)
#   4. frontend type-check + build (tsc -b && vite build)
#   5. frontend unit tests     (vitest run)
#
# Added per the production-readiness audit (finding F-4): different
# environments were silently omitting whole test groups (missing fastapi ->
# 31 modules collected as errors; missing git -> the transport-centralization
# guard fails; API tests skipped without a server). This script makes every
# omission LOUD and actionable instead of silent.
#
# Usage:
#   ./validate.sh            # full validation (canonical, use before promotion)
#   ./validate.sh --backend  # backend-only (fast inner loop; NOT promotion-grade)
#
# Exit code: 0 only if every executed phase passed.
# ═══════════════════════════════════════════════════════════════════════════
set -u -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_ONLY="${1:-}"
FAILURES=()
API_PORT="${VALIDATE_API_PORT:-8765}"

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
fail() { FAILURES+=("$1"); printf '\033[31mFAIL: %s\033[0m\n' "$1"; }
need() { # need <cmd> <why / how to fix>
  if ! command -v "$1" >/dev/null 2>&1; then
    fail "missing requirement: '$1' — $2"
    return 1
  fi
}

# ── 1. Environment preflight — every requirement checked, none silently skipped
say "1/5 environment preflight"
need python3 "install Python 3.10+ (backend + live node tests)"
need git     "required by the transport-centralization architecture guard (test_transport.py uses 'git grep'); install git or run from a git checkout"
need node    "required for frontend type-check/build/tests; install Node 20+"
need npm     "required for frontend type-check/build/tests"
python3 - <<'PY' || fail "python deps: install with  cd backend && pip install -r requirements.txt  (plus: pip install pytest pytest-xdist)"
missing = []
for mod in ("pytest", "xdist", "fastapi", "httpx", "pandas", "numpy"):
    try:
        __import__(mod)
    except Exception:
        missing.append(mod)
if missing:
    raise SystemExit(f"missing python modules: {missing}")
PY
if [ ! -e "$ROOT/.git" ]; then
  fail "not a git checkout — the transport-centralization guard cannot run; validate from the real repository/worktree"
fi
[ ${#FAILURES[@]} -gt 0 ] && { say "environment not ready — fix the items above and re-run"; printf '%s\n' "${FAILURES[@]}"; exit 2; }
echo "environment OK"

# ── 2. Backend suite (excludes the server-dependent API module; that runs in 3)
say "2/5 backend test suite"
( cd "$ROOT/backend" && python3 -m pytest tests/ --ignore=tests/test_control_tower_api.py -q ) \
  || fail "backend test suite"

# ── 3. API contract tests against a real local server (always cleaned up).
#     CONTROL_TOWER_STATE_DIR (backend/state_dir.py) points ALL durable server
#     state (execution/scenario/ledger sqlite, events, runtime overlays) at a
#     throwaway dir so validation never writes state inside the repository.
say "3/5 API contract tests (local uvicorn on 127.0.0.1:${API_PORT})"
API_LOG="$(mktemp)"
API_STATE="$(mktemp -d)"
# `exec` makes uvicorn REPLACE the subshell, so $! below is uvicorn's own PID —
# without it, kill "$UVICORN_PID" would kill only the subshell and orphan the
# server, leaving the port occupied for the next run (governance audit G-1).
( cd "$ROOT/backend" && CONTROL_TOWER_STATE_DIR="$API_STATE" LIVE_STATE_DIR="$API_STATE/live_state" \
    exec python3 -m uvicorn server:app --host 127.0.0.1 --port "$API_PORT" >"$API_LOG" 2>&1 ) &
UVICORN_PID=$!
trap 'kill "$UVICORN_PID" 2>/dev/null; wait "$UVICORN_PID" 2>/dev/null' EXIT
up=""
for _ in $(seq 1 40); do
  if python3 - "$API_PORT" <<'PY' 2>/dev/null; then up=1; break; fi
import sys, urllib.request
urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/", timeout=1)
PY
  sleep 0.5
done
if [ -z "$up" ]; then
  fail "API server failed to start (log: $API_LOG)"
else
  ( cd "$ROOT/backend" && REACT_APP_BACKEND_URL="http://127.0.0.1:${API_PORT}" \
      python3 -m pytest tests/test_control_tower_api.py -q ) \
    || fail "API contract tests"
fi
kill "$UVICORN_PID" 2>/dev/null; wait "$UVICORN_PID" 2>/dev/null; trap - EXIT

if [ "$BACKEND_ONLY" = "--backend" ]; then
  say "backend-only mode: frontend phases SKIPPED (not promotion-grade)"
else
  # ── 4. Frontend type-check + build
  say "4/5 frontend type-check + build (tsc -b && vite build)"
  if [ ! -d "$ROOT/frontend/node_modules" ]; then
    fail "frontend/node_modules missing — run:  cd frontend && npm ci"
  else
    ( cd "$ROOT/frontend" && npm run build ) || fail "frontend type-check/build"
    # ── 5. Frontend unit tests
    say "5/5 frontend unit tests (vitest run)"
    ( cd "$ROOT/frontend" && npm test ) || fail "frontend unit tests"
  fi
fi

# ── Summary
say "validation summary"
if [ ${#FAILURES[@]} -eq 0 ]; then
  echo "ALL VALIDATION PHASES PASSED"
  exit 0
else
  printf 'FAILED PHASES (%d):\n' "${#FAILURES[@]}"
  printf '  - %s\n' "${FAILURES[@]}"
  exit 1
fi
