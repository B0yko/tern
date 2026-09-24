#!/usr/bin/env bash
# Demo workspace setup: indexes whatever demo media is under demo/ into
# demo/db/ through the API, then runs one search as a smoke test.
#
# No demo media is committed to this repository. Fetch it first with any of
#   demo/demo_reel/download.sh     20 camera-named photos and videos
#   demo/real_photos/download.sh   7 photos
#   demo/real_videos/download.sh   2 videos (needs yt-dlp)
# The script stops with that instruction when it finds no media.
#
# Indexing the three sets (29 files, about 40 minutes of video) takes a couple
# of minutes on Apple Silicon. The first run also downloads the Whisper Q5
# model (~547 MB) into service_pipeline/models/ if it is missing, and the
# SigLIP-2 weights (~1.5 GB) into the Hugging Face cache. Indexing goes
# through /api/index, so it counts against the 120-minute trial like any
# other indexing done through the app.
#
# Stop ./run.sh first if it is serving demo/: ChromaDB allows one process
# per workspace, and the script refuses to start next to one.
#
# Usage: scripts/init_demo.sh

set -uo pipefail
HERE="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ROOT="$(dirname "$HERE")"
DEMO="$ROOT/demo"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

bold "── Tern demo workspace setup ──"

# Sanity: are the demo media files there?
if [ ! -d "$DEMO" ]; then
  red "  demo/ folder missing — did you clone the full repo?"
  exit 1
fi
# Count media outside demo/db (thumbnails) and demo/exports (clip exports),
# the two folders the indexer itself skips.
FILES=$(find "$DEMO" \( -path "$DEMO/db" -o -path "$DEMO/exports" \) -prune -o \
  -type f \( -name "*.mp3" -o -name "*.mp4" -o -name "*.jpg" -o -name "*.png" -o -name "*.heic" \) -print \
  | wc -l | tr -d ' ')
if [ "$FILES" -eq 0 ]; then
  red "  no demo media under demo/ — it is not committed to the repository."
  echo "    fetch it first, from the repository root:"
  echo "      demo/demo_reel/download.sh"
  echo "      demo/real_photos/download.sh"
  echo "      demo/real_videos/download.sh   (needs yt-dlp)"
  echo "    then re-run scripts/init_demo.sh"
  exit 1
fi
green "  found $FILES media files in demo/"

# Sanity: are the binary deps installed?
MISSING=""
command -v ffmpeg >/dev/null     || MISSING="$MISSING ffmpeg"
command -v whisper-cli >/dev/null || MISSING="$MISSING whisper-cli"
command -v uv >/dev/null         || MISSING="$MISSING uv"
if [ -n "$MISSING" ]; then
  red "  missing required tools:$MISSING"
  echo "    install via:  brew install ffmpeg whisper-cpp && curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi
green "  ffmpeg, whisper-cli, uv all present"

# Sanity: vision-ocr binary
if [ ! -x "$ROOT/service_pipeline/bin/vision-ocr" ]; then
  echo "  building vision-ocr Swift binary…"
  (cd "$ROOT/service_pipeline" && swiftc -O bin/vision-ocr.swift -o bin/vision-ocr)
fi
green "  vision-ocr ready"

# Refuse to run next to a backend that is already serving demo/ (./run.sh
# defaults to it): a second process opening the same ChromaDB collection can
# corrupt it, and the re-index below deletes demo/db/.
DEMO_REAL="$(cd "$DEMO" && pwd -P)"
for candidate in 18765 18766 18767 18768 18769 8765 8766 8767; do
  LIVE_WS=$(curl -s --max-time 2 "http://127.0.0.1:$candidate/api/health" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('workspace',''))" 2>/dev/null)
  if [ -n "$LIVE_WS" ] && [ "$LIVE_WS" = "$DEMO_REAL" ]; then
    red "  a Tern backend on port $candidate is already serving demo/ — stop ./run.sh and re-run"
    exit 1
  fi
done

# If the demo DB already exists, ask before re-indexing.
if [ -f "$DEMO/db/tern.db" ]; then
  read -p "  demo/db/tern.db already exists. Re-index from scratch? [y/N] " yn
  if [[ ! "$yn" =~ ^[Yy]$ ]]; then
    green "  skipping — demo DB preserved"
    exit 0
  fi
  rm -rf "$DEMO/db/"
fi

bold ""
bold "── starting backend ──"

# Auto-find a free port, the same way dev_check.sh does. A hardcoded 8765
# failed in two ways:
#   1. Something else on 8765 answered /api/health → the script thought
#      the backend was up → posted /api/index to the WRONG backend →
#      "Folder not found" → zero indexing progress with no clear error.
#   2. Bind conflict → uvicorn dies → init_demo hangs in the readiness
#      poll for 30s → cryptic timeout.
# Prefer 18765, the same default as run.sh and the Tauri shell.
# TERN_DEMO_PORT overrides.
TERN_PORT="${TERN_DEMO_PORT:-}"
if [ -z "$TERN_PORT" ]; then
  for candidate in 18765 18766 18767 18768 18769 8765 8766 8767; do
    if python3 -c "import socket,sys; s=socket.socket(); s.bind(('127.0.0.1',$candidate)); s.close()" 2>/dev/null; then
      TERN_PORT=$candidate
      break
    fi
  done
fi
[ -n "$TERN_PORT" ] || { red "  no free port found in 18765-8767 range"; exit 1; }
BASE_URL="http://127.0.0.1:$TERN_PORT"
echo "  using port: $TERN_PORT"

cd "$ROOT/api"
uv sync >/dev/null 2>&1
TERN_WORKSPACE="$DEMO" uv run uvicorn main:app --host 127.0.0.1 --port "$TERN_PORT" > /tmp/tern_init.log 2>&1 &
BACKEND_PID=$!
# Trap kills BOTH children + the python parent. Without pkill -P, the
# multiprocessing resource_tracker + any in-flight ffmpeg/whisper that
# python spawned during the demo-indexing run would orphan-leak to
# launchd and keep the port bound for ~30s after script exit —
# breaking the contributor's NEXT init_demo run. Same bug class as
# the sidecar teardown in the Tauri shell.
trap "pkill -P $BACKEND_PID 2>/dev/null; kill $BACKEND_PID 2>/dev/null" EXIT
for i in {1..30}; do
  if curl -sf "$BASE_URL/api/health" >/dev/null 2>&1; then
    green "  backend up after ${i}s (pid=$BACKEND_PID)"
    break
  fi
  sleep 1
  if [ "$i" = "30" ]; then
    red "  backend never came up — log tail:"
    tail -30 /tmp/tern_init.log
    exit 1
  fi
done

bold ""
bold "── indexing demo/ ──"
# Hit /api/index with the demo workspace path. force=true on fresh DB ensures
# nothing stale lingers.
# /api/index refuses with 402 when the trial is spent, 424 when a binary is
# missing, and ok:false when it finds no media. Surface all three instead of
# polling a run that never started.
INDEX_RESP=$(curl -s -w '\n%{http_code}' -X POST "$BASE_URL/api/index" \
  -H 'Content-Type: application/json' \
  -d "{\"folder\":\"$DEMO\",\"force\":true}")
INDEX_CODE=$(echo "$INDEX_RESP" | tail -n 1)
INDEX_BODY=$(echo "$INDEX_RESP" | sed '$d')
INDEX_OK=$(echo "$INDEX_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok'))" 2>/dev/null)
if [ "$INDEX_CODE" != "200" ] || [ "$INDEX_OK" != "True" ]; then
  red "  /api/index refused (HTTP $INDEX_CODE):"
  echo "    $INDEX_BODY"
  exit 1
fi
echo "  indexing started — this takes a couple of minutes on Apple Silicon."

# Poll until done — with a HARD TIMEOUT. Previously this loop had none
# so a hung Whisper subprocess would keep the script alive forever.
# 10 min is generous: the three demo sets (29 files) index in about a
# minute and a half on Apple Silicon.
POLL_TIMEOUT_S=600
POLL_DEADLINE=$(( $(date +%s) + POLL_TIMEOUT_S ))
while true; do
  S=$(curl -s --max-time 5 "$BASE_URL/api/index/status" 2>/dev/null)
  RUNNING=$(echo "$S" | python3 -c "import sys,json; print(json.load(sys.stdin).get('running'))" 2>/dev/null)
  DONE=$(echo "$S" | python3 -c "import sys,json; print(json.load(sys.stdin).get('files_done',0))" 2>/dev/null)
  TOTAL=$(echo "$S" | python3 -c "import sys,json; print(json.load(sys.stdin).get('files_pending',0))" 2>/dev/null)
  printf "\r  progress: %s / %s" "$DONE" "$TOTAL"
  [ "$RUNNING" = "False" ] && break
  if [ "$(date +%s)" -ge "$POLL_DEADLINE" ]; then
    echo ""
    red "  timed out after ${POLL_TIMEOUT_S}s waiting for indexing — log tail:"
    tail -50 /tmp/tern_init.log
    exit 1
  fi
  sleep 3
done
echo ""
green "  indexing done"

bold ""
bold "── smoke test ──"
# Pick a query that matches whichever demo set was downloaded.
if   [ -f "$DEMO/real_videos/yc_lecture1_intro.mp4" ]; then SMOKE_Q="stanford"
elif [ -f "$DEMO/demo_reel/DSC_4471.jpg" ];           then SMOKE_Q="range rover"
elif [ -f "$DEMO/real_photos/cat_portrait.jpg" ];     then SMOKE_Q="orange cat"
else                                                       SMOKE_Q="stanford"
fi
COUNT=$(curl -s -X POST "$BASE_URL/api/search" \
  -H 'Content-Type: application/json' \
  -d "{\"query\":\"$SMOKE_Q\",\"limit\":5}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])" 2>/dev/null)
if [ "${COUNT:-0}" -gt 0 ]; then
  green "  search '$SMOKE_Q' returned $COUNT hits — demo ready!"
  echo ""
  echo "  Next steps:"
  echo "    1. ./run.sh  (starts the backend on demo/ and opens the browser;"
  echo "       this script's backend stops when it exits)"
  echo "    2. or  scripts/dev_check.sh  (verify everything)"
else
  red "  search '$SMOKE_Q' returned 0 hits — something went wrong"
  exit 1
fi
