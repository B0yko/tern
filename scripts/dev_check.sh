#!/usr/bin/env bash
# One-command pre-push check. Runs:
#   1. version-source consistency (api/pyproject.toml, Cargo.toml,
#      tauri.conf.json)
#   2. a grep for escape patterns that were XSS sinks in app/modules/
#   3. pytest, service_pipeline
#   4. pytest, api
#   5. backend up on the first free port from 18765 (default) onwards —
#      see the port-search loop below; mirrors run.sh and the Rust shell
#   6. qa_smoke.py end-to-end against that port
# Exits 0 only if all green.

set -uo pipefail
HERE="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ROOT="$(dirname "$HERE")"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

bold "── version-source consistency check ────────────────────"
# Three independent files hold the project version: the python API's
# pyproject.toml, the Rust shell's Cargo.toml, and the Tauri bundler's
# tauri.conf.json. They MUST agree at release time or:
#   - tauri-bundler emits the .dmg with the conf.json version, but
#     release.sh derives its expected filename from the same file,
#     so that one stays consistent
#   - /api/diagnostics reads pyproject.toml —
#     drift → diagnostics report old version, license server
#     receives old version on activation, support gets confusing
#     bug reports against the wrong build
#   - Cargo.toml carries the shell crate's own version; keeping it equal
#     means one number identifies a build everywhere
# Force them to match in one place so a half-bumped release fails
# loudly here instead of silently in production.
VERSIONS=$(python3 - "$ROOT" <<'PYEOF'
import json, re, sys
root = sys.argv[1]
def py_ver(path):
    for line in open(path):
        m = re.match(r'\s*version\s*=\s*"([^"]+)"', line)
        if m: return m.group(1)
def cargo_ver(path):
    for line in open(path):
        m = re.match(r'\s*version\s*=\s*"([^"]+)"', line)
        if m: return m.group(1)
def conf_ver(path):
    return json.load(open(path))["version"]
v_py   = py_ver(f"{root}/api/pyproject.toml")
v_cgo  = cargo_ver(f"{root}/tauri/src-tauri/Cargo.toml")
v_conf = conf_ver(f"{root}/tauri/src-tauri/tauri.conf.json")
print(f"pyproject.toml={v_py}")
print(f"Cargo.toml={v_cgo}")
print(f"tauri.conf.json={v_conf}")
print(f"AGREE={int(v_py == v_cgo == v_conf)}")
PYEOF
)
AGREE=$(echo "$VERSIONS" | grep '^AGREE=' | cut -d= -f2)
if [ "$AGREE" != "1" ]; then
  red "  ✗ version drift across source-of-truth files:"
  echo "$VERSIONS" | grep -v '^AGREE=' | sed 's/^/    /'
  red "  Pick one canonical value, update all three, retry."
  exit 1
fi
echo "$VERSIONS" | grep -v '^AGREE=' | head -1 | sed 's/^pyproject.toml=/  /'
green "  versions consistent"

bold ""
bold "── frontend XSS regression scan ────────────────────────"
# Catch reintroduction of the insufficient-escape patterns we burned out
# of the JS modules. Both were real XSS sinks before:
#   - `.replace(/"/g, "&quot;")` only escapes the attribute quote and
#     leaves `<`, `>`, `&`, `'` raw — fine for the simplest attribute
#     case but unsafe the moment the same string lands in element text
#     (sidebar.js folder labels did exactly that).
#   - `.replace(/[<>&]/g, "")` strips dangerous chars instead of
#     escaping them, which silently corrupts legitimate filenames that
#     contain `&` (and still leaves single/double quotes raw in
#     attribute context).
# The canonical helper is a 5-char regex escape (`&<>"'`) — used in
# results.js, suggest.js, row.js, sidebar.js, empty.js, filters.js,
# indexing.js, license.js. Anything else is a regression.
# Exclude `*\ 2.js` IDE backup duplicates and the comments in this script.
XSS_HITS=$(grep -rEn 'replace\(/\"/g, "&quot;"\)|replace\(/\[<>&\]/g, ""\)' \
  "$ROOT/app/modules/" 2>/dev/null \
  | grep -v ' 2.js:' | grep -v '^[^:]*://' | grep -v '^[^:]*:.*//.*replace')
if [ -n "$XSS_HITS" ]; then
  red "  ✗ insufficient-escape pattern reintroduced:"
  echo "$XSS_HITS" | sed 's/^/    /'
  red "  Use the project-standard _esc() helper instead."
  exit 1
fi
green "  no insufficient-escape patterns in app/modules/"

bold ""
bold "── pytest (service_pipeline) ──────────────────────────"
cd "$ROOT/service_pipeline"
if uv run pytest -q; then
  green "  service_pipeline pytest: passing"
else
  red "  service_pipeline pytest: FAILED — fix before pushing"
  exit 1
fi

bold ""
bold "── pytest (api — security + polish regressions) ───────"
# Pin the venv at $HOME/.local/venvs/tern-api, outside iCloud Drive
# (see "_editable_impl_tern_service.pth" in docs/TROUBLESHOOTING.md).
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.local/venvs/tern-api}"
cd "$ROOT/api"
if uv run pytest -q tests/; then
  green "  api pytest: passing"
else
  red "  api pytest: FAILED — fix before pushing"
  exit 1
fi

bold ""
bold "── backend ─────────────────────────────────────────────"

# Auto-find a free port — mirrors the Rust shell's find_free_port in
# tauri/src-tauri/src/main.rs. Previously dev_check hardcoded 8765 and
# would either:
#   a) silently run qa_smoke against whatever other service or Tern
#      instance happened to be on 8765 → false-green or noisy false-red, or
#   b) fail to bind uvicorn → cryptic timeout 30 s later.
# Prefer 18765 (memorable, high-range, almost never taken), the same
# default as run.sh and the Tauri shell.
TERN_PORT="${TERN_DEV_PORT:-}"
if [ -z "$TERN_PORT" ]; then
  for candidate in 18765 18766 18767 18768 18769 8765 8766 8767; do
    # Use python3 (already required for qa_smoke) for cross-platform
    # listen test — `nc -z` flags vary between macOS and Linux/BusyBox.
    if python3 -c "import socket,sys; s=socket.socket(); s.bind(('127.0.0.1',$candidate)); s.close()" 2>/dev/null; then
      TERN_PORT=$candidate
      break
    fi
  done
fi
[ -n "$TERN_PORT" ] || { red "  no free port found in 18765-8767 range"; exit 1; }
echo "  using port: $TERN_PORT"

BASE_URL="http://127.0.0.1:$TERN_PORT"
STARTED_BY_US=0
LOG_FILE="/tmp/tern_devcheck_${TERN_PORT}.log"

if curl -sf "$BASE_URL/api/health" >/dev/null 2>&1; then
  green "  backend already running on :$TERN_PORT, reusing"
else
  echo "  starting backend on :$TERN_PORT…"
  cd "$ROOT/api"
  uv run uvicorn main:app --host 127.0.0.1 --port "$TERN_PORT" > "$LOG_FILE" 2>&1 &
  BACKEND_PID=$!
  STARTED_BY_US=1
  for i in {1..30}; do
    if curl -sf "$BASE_URL/api/health" >/dev/null 2>&1; then
      green "  backend up after ${i}s"
      break
    fi
    sleep 1
    if [ "$i" = "30" ]; then
      red "  backend never came up — log tail:"
      tail -30 "$LOG_FILE"
      exit 1
    fi
  done
fi

bold ""
bold "── qa_smoke.py ─────────────────────────────────────────"
cd "$ROOT"
if python3 scripts/qa_smoke.py --base "$BASE_URL"; then
  green ""
  green "  ✓ ALL CHECKS PASS"
  RESULT=0
else
  red ""
  red "  ✗ QA SMOKE FAILED"
  RESULT=1
fi

# Tear down only what we started — and only OUR process, not any other
# uvicorn the developer might have running for unrelated work. Two-step
# takedown: kill the CHILDREN of the uvicorn python first (multiprocessing
# resource_tracker, any in-flight ffmpeg/whisper that python launched),
# then the python parent itself. Without the pkill -P step the children
# get reparented to launchd and orphan-leak — same bug class as the
# sidecar teardown in the Tauri shell.
if [ "$STARTED_BY_US" = "1" ] && [ -n "${BACKEND_PID:-}" ]; then
  pkill -P "$BACKEND_PID" 2>/dev/null || true
  kill "$BACKEND_PID" 2>/dev/null || true
  # Wait briefly so the next dev_check run gets the port back cleanly.
  wait "$BACKEND_PID" 2>/dev/null || true
fi

exit $RESULT
