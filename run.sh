#!/usr/bin/env bash
# Tern — one-command dev launcher.
#
#   ./run.sh                 # demo workspace, first free port from 18765
#   TERN_PORT=18800 ./run.sh # pin the port
#   TERN_WORKSPACE=/path/to/archive ./run.sh
#
# Starts the FastAPI backend and opens the browser at it. The Tauri shell
# (tauri/) is the packaged path; this is the no-build path for development.
#
# Port choice mirrors the Rust shell's find_free_port() and dev_check.sh so
# every entry point agrees on one default. 8765 collided with other dev
# tooling in the field, hence the 18765 high-range default.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT/api"

export TERN_WORKSPACE="${TERN_WORKSPACE:-$ROOT/demo}"

# --- port selection -----------------------------------------------------
if [ -z "${TERN_PORT:-}" ]; then
  for candidate in 18765 18766 18767 18768 18769 8765 8766 8767; do
    if python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',$candidate)); s.close()" 2>/dev/null; then
      TERN_PORT=$candidate
      break
    fi
  done
fi
if [ -z "${TERN_PORT:-}" ]; then
  echo "No free port in 18765-18769 / 8765-8767. Free one or set TERN_PORT." >&2
  exit 1
fi
export TERN_PORT

# --- runtime ------------------------------------------------------------
# uv manages the api venv (see api/pyproject.toml; tern-service is an
# editable path dep on ../service_pipeline, so one sync covers both).
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Install it: brew install uv" >&2
  exit 1
fi

echo "Tern"
echo "  workspace : $TERN_WORKSPACE"
echo "  port      : $TERN_PORT"
echo "  docs      : http://127.0.0.1:$TERN_PORT/docs"
echo

# Open the browser once the port answers, without blocking the server start.
(
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$TERN_PORT/api/health" >/dev/null 2>&1; then
      open "http://127.0.0.1:$TERN_PORT" 2>/dev/null || true
      exit 0
    fi
    sleep 0.5
  done
) &

exec uv run uvicorn main:app --host 127.0.0.1 --port "$TERN_PORT"
