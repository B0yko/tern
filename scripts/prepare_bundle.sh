#!/usr/bin/env bash
# Prep the tauri/src-tauri/resources/ directory before a
# `cargo tauri build`. Populates:
#   - resources/bin/        (ffmpeg, ffprobe, whisper-cli, vision-ocr — dylib-bundled)
#   - resources/libs/       (transitive dylibs)
#   - resources/ggml-backend/  (whisper.cpp ggml plugins)
#   - resources/models/     (Whisper Q5 model)
#   - resources/python/     (CPython 3.11 interpreter + full api venv site-packages)
#
# Idempotent: re-running skips work that's already done.
# Required tools on the build machine: brew, uv, dylibbundler, swiftc, curl.

set -uo pipefail
HERE="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ROOT="$(dirname "$HERE")"
RES="$ROOT/tauri/src-tauri/resources"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

bold "── Prep .app resources ──"
mkdir -p "$RES/bin" "$RES/libs" "$RES/ggml-backend" "$RES/models" "$RES/python" "$RES/api"

# ── 1. vision-ocr (build from Swift source if missing) ─────────────────────
if [ ! -x "$RES/bin/vision-ocr" ]; then
  echo "  building vision-ocr from Swift…"
  (cd "$ROOT/service_pipeline" && swiftc -O bin/vision-ocr.swift -o bin/vision-ocr)
  cp "$ROOT/service_pipeline/bin/vision-ocr" "$RES/bin/"
  chmod +x "$RES/bin/vision-ocr"
fi
green "  vision-ocr ready ($(du -h $RES/bin/vision-ocr | cut -f1))"

# ── 2. ffmpeg + ffprobe (LGPL build — NOT Homebrew) ────────────────────────
#
# This used to copy from `brew --prefix ffmpeg`. Homebrew builds with
# --enable-gpl and libx264/libx265, and putting that inside a closed-source
# app makes the whole product GPL — meaning we would owe Tern's source to
# every customer. scripts/build_ffmpeg_lgpl.sh produces an equivalent
# ffmpeg with no GPL component; this copies its output.
#
# There is no Homebrew fallback on purpose. A fallback here fails silently
# into a licence violation that nobody notices until it matters.
FF_STAGE="$ROOT/third_party/ffmpeg/build/stage"
if [ ! -x "$RES/bin/ffmpeg" ]; then
  if [ ! -x "$FF_STAGE/bin/ffmpeg" ]; then
    red "  no LGPL ffmpeg at $FF_STAGE"
    red "  run: scripts/build_ffmpeg_lgpl.sh"
    red "  (do NOT substitute the Homebrew ffmpeg — it is GPL)"
    exit 1
  fi
  cp "$FF_STAGE/bin/ffmpeg" "$FF_STAGE/bin/ffprobe" "$RES/bin/"
  cp "$FF_STAGE/libs/"*.dylib "$RES/libs/"
  chmod +x "$RES/bin/ffmpeg" "$RES/bin/ffprobe"
fi

# Refuse to build a bundle around a GPL ffmpeg, however it got there.
if "$RES/bin/ffmpeg" -hide_banner -version 2>/dev/null | grep -q -- "--enable-gpl"; then
  red "  ✗ bundled ffmpeg reports --enable-gpl — refusing to package it"
  red "    Delete $RES/bin/ffmpeg and re-run scripts/build_ffmpeg_lgpl.sh"
  exit 1
fi
if "$RES/bin/ffmpeg" -hide_banner -encoders 2>/dev/null | grep -qE "libx264|libx265"; then
  red "  ✗ bundled ffmpeg contains a GPL encoder — refusing to package it"
  exit 1
fi
green "  ffmpeg + ffprobe ready, LGPL ($(ls $RES/libs/ | wc -l | tr -d ' ') dylibs)"

# ── 2b. licence notices must travel with the binaries ──────────────────────
mkdir -p "$RES/licenses"
cp "$ROOT/third_party/NOTICE.md" "$RES/licenses/" 2>/dev/null || true
cp "$ROOT/third_party/ffmpeg/COPYING.LGPLv2.1" "$RES/licenses/" 2>/dev/null || true
rm -f "$RES/licenses/lame-LICENSE"   # old flat name, before third_party/lame/
mkdir -p "$RES/licenses/lame"
cp "$ROOT/third_party/lame/COPYING" "$ROOT/third_party/lame/README-LICENSE" \
  "$RES/licenses/lame/" 2>/dev/null || true
[ -f "$RES/licenses/COPYING.LGPLv2.1" ] || { red "  ✗ LGPL licence text missing"; exit 1; }
green "  licence notices staged ($(ls $RES/licenses | wc -l | tr -d ' ') files)"

# ── 3. whisper-cli + ggml backends ─────────────────────────────────────────
if [ ! -x "$RES/bin/whisper-cli" ]; then
  WHISPER_HOME="$(brew --prefix whisper-cpp)"
  GGML_HOME="$(brew --prefix ggml)"
  [ -d "$WHISPER_HOME" ] || { red "  whisper-cpp not installed via brew"; exit 1; }
  cp "$WHISPER_HOME/bin/whisper-cli" "$RES/bin/"
  chmod +x "$RES/bin/whisper-cli"
  dylibbundler -of -b -x "$RES/bin/whisper-cli" -d "$RES/libs/" \
    -p '@executable_path/../libs/' \
    -s "$WHISPER_HOME/lib" -s "$GGML_HOME/lib" >/dev/null 2>&1
  cp "$GGML_HOME/libexec/"*.so "$RES/ggml-backend/"
  for so in "$RES/ggml-backend/"*.so; do
    dylibbundler -of -b -x "$so" -d "$RES/libs/" -p '@loader_path/../libs/' -s "$GGML_HOME/lib" >/dev/null 2>&1
  done
fi
green "  whisper-cli + ggml backends ready"

# ── 4. Whisper Q5 model (downloads ~547 MB on first run) ───────────────────
# Real bug previously: script runs with `set -uo pipefail` (no -e), so if
# curl errored partway through (network blip, HF 503, Ctrl-C right before
# completion), the partial .part file got renamed to the target. Next
# build saw a non-zero $MODEL and skipped re-download → shipped a
# corrupted model that fails Whisper load with a cryptic "magic number
# mismatch" at runtime. Fix: --fail on curl so HTTP errors propagate,
# check exit code before renaming, AND verify the final file is at
# least 400 MB (full Q5 binary is 547 MB; <400 MB is definitely
# truncated).
MODEL="$RES/models/ggml-large-v3-turbo-q5_0.bin"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin"
MODEL_MIN_BYTES=$((400 * 1024 * 1024))  # 400 MB — full model is ~547 MB
MODEL_SIZE=$(stat -f%z "$MODEL" 2>/dev/null || echo 0)
if [ ! -s "$MODEL" ] || [ "$MODEL_SIZE" -lt "$MODEL_MIN_BYTES" ]; then
  echo "  downloading Whisper Q5 model (547 MB)…"
  # Defensive: nuke any stale .part / truncated $MODEL from a previous
  # failed run so we never confuse leftover bytes with valid resume.
  rm -f "$MODEL.part" "$MODEL"
  if ! curl -L --progress-bar --fail -o "$MODEL.part" "$MODEL_URL"; then
    rm -f "$MODEL.part"
    red "  ✗ Whisper model download failed (curl error). Check network / HF status, then retry."
    exit 1
  fi
  # Verify the .part is plausible-size before renaming. Catches the
  # rare case where curl reports success but only partial bytes
  # transferred (truncated body, MITM proxy, etc.).
  DOWNLOADED=$(stat -f%z "$MODEL.part" 2>/dev/null || echo 0)
  if [ "$DOWNLOADED" -lt "$MODEL_MIN_BYTES" ]; then
    rm -f "$MODEL.part"
    red "  ✗ Whisper model truncated (${DOWNLOADED} bytes < 400 MB minimum). Retry."
    exit 1
  fi
  mv "$MODEL.part" "$MODEL"
fi
green "  Whisper model ready ($(du -h $MODEL | cut -f1))"

# ── 5. Python interpreter + api venv site-packages ─────────────────────────
PY_BIN="$RES/python/bin/python3.11"
if [ ! -x "$PY_BIN" ]; then
  echo "  copying CPython 3.11 (uv-managed)…"
  PY_SRC="$(uv python find 3.11 | xargs dirname | xargs dirname)"
  [ -d "$PY_SRC" ] || { red "  no uv-managed 3.11 found (try: uv python install 3.11)"; exit 1; }
  cp -R "$PY_SRC/"* "$RES/python/"

  echo "  syncing api deps + copying site-packages (~1.2 GB)…"
  # Force uv to use the external venv (out of iCloud — see run.sh comment).
  # `uv sync` always writes to UV_PROJECT_ENVIRONMENT if set.
  (cd "$ROOT/api" && \
    UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.local/venvs/tern-api}" \
    uv sync >/dev/null)
  SP_SRC="${UV_PROJECT_ENVIRONMENT:-$HOME/.local/venvs/tern-api}/lib/python3.11/site-packages"
  # Fall back to legacy in-project location if external venv missing (older clones)
  [ -d "$SP_SRC" ] || SP_SRC="$ROOT/api/.venv/lib/python3.11/site-packages"
  rsync -a "$SP_SRC/" "$RES/python/lib/python3.11/site-packages/"

fi

# ALWAYS refresh the bundled `tern` package (idempotent, ~14 KB copy).
# Was previously inside the `if [ ! -x "$PY_BIN" ]` guard, which meant
# subsequent rebuilds shipped a stale `tern/` package — code changes to
# the service_pipeline (e.g. discover_files _SKIP_DIRS fix, 807d696)
# wouldn't reach the .app until someone deleted python/ manually.
PYSP="$RES/python/lib/python3.11/site-packages"
rm -f "$PYSP/_editable_impl_tern_service.pth"
rm -rf "$PYSP/tern"
cp -R "$ROOT/service_pipeline/tern" "$PYSP/tern"

# Pre-compile .pyc for the bundled interpreter. First launch on a customer
# Mac otherwise hangs for ~3 min in _PyCodecRegistry_Init → os_replace while
# Spotlight/XprotectService scans the freshly-copied 1.2 GB Python tree and
# the interpreter tries to atomically write __pycache__/*.pyc concurrently.
# Idempotent: compileall skips up-to-date .pyc. `|| true` because some
# vendored deps carry intentional Python-2 / future-syntax .py files that
# always fail to compile under 3.11 — those aren't blockers for runtime.
echo "  pre-compiling .pyc (first-launch race fix)…"
"$PY_BIN" -m compileall -q -j 4 "$RES/python/lib/python3.11" 2>/dev/null || true

# Defensive: clean any orphan `*.pyc.NNNN` temp files (Python writes these
# during atomic-rename; if a previous launch was force-killed mid-write or
# Spotlight raced the rename, they persist and deadlock the next launch in
# _PyCodecRegistry_Init → os_replace). Cheap; runs every build.
find "$RES/python/lib" -name '*.pyc.[0-9]*' -delete 2>/dev/null || true

# Strip macOS extended attributes (com.apple.quarantine, com.apple.metadata:*)
# from the bundled resources so Spotlight doesn't re-scan + race the .pyc
# writes on first launch of the rebuilt .app. xattr -cr is fast (single
# walk; flag clear).
xattr -cr "$RES/python" 2>/dev/null || true
xattr -cr "$RES/api"    2>/dev/null || true
xattr -cr "$RES/app"    2>/dev/null || true

green "  Python bundle ready ($(du -sh $RES/python | cut -f1))"

# ── 6. api/ source code (uvicorn entrypoint) + app/ frontend ──────────────
# Tauri's bundle copies our resources/ verbatim, so these land at
# <Tern.app>/Contents/Resources/resources/{api,app}/ — discovered by
# the Rust sidecar's api_dir lookup chain.
#
# `--exclude='* [0-9].*'` and `--exclude='* [0-9]'` filter iCloud Drive's
# duplicate-suffix carcasses (api 2.js, main 2.js, shell 2.css, …).
# When the project lives in ~/Documents/ on a Mac with iCloud Drive on
# (which the README's dev setup explicitly is), iCloud creates ` 2`-suffix
# shadow copies of any file that gets touched while sync is paused; they
# stay untracked in git but ARE in the working tree. Without these excludes
# rsync copies them into the .app and the released bundle ships e.g.
# `app/main 2.js` + `app/modules/state 2.js` alongside the real files
# (verified locally: 17 such files in app/modules/, 13 in app/, plus
# conftest 2.py + test_transcript_window 2.py in api/tests/). They're
# unreachable from the live app's <script> tags, but they bloat the
# bundle, leak the dev machine's iCloud state into customer downloads,
# and could be loaded directly via the sidecar's static-file server if
# someone hardcodes the URL — a stale `main 2.js` masquerading as the
# live entry point would be a real debugging nightmare.
rsync -a --delete \
  --exclude='.venv' --exclude='__pycache__' --exclude='.python-version' \
  --exclude='* [0-9].*' --exclude='* [0-9]' \
  "$ROOT/api/" "$RES/api/"
rsync -a --delete \
  --exclude='.DS_Store' \
  --exclude='* [0-9].*' --exclude='* [0-9]' \
  "$ROOT/app/" "$RES/app/"
green "  api/ + app/ ready ($(du -sh $RES/api | cut -f1), $(du -sh $RES/app | cut -f1))"

# ── 7. demo workspace (pre-indexed example archive, ~200 MB) ───────────────
# Gives the customer a "drag .dmg → search 'stanford' → see results" first
# impression. exports/ is excluded (runtime artifact); db/ + media files
# included so the workspace is fully functional out of the box.
# iCloud-dupe excludes match the api/ + app/ pattern — demo/ also lives
# under ~/Documents/ on a dev Mac with iCloud Drive on, so the same
# ` 2.*` shadow files appear there (e.g. `tern 2.db`, `real_videos/
# README 2.md` observed in the wild). Without the excludes, every
# `cargo tauri build` shipped them into the customer .dmg.
rsync -a --delete \
  --exclude='.DS_Store' --exclude='exports' \
  --exclude='* [0-9].*' --exclude='* [0-9]' \
  "$ROOT/demo/" "$RES/demo/"
green "  demo workspace ready ($(du -sh $RES/demo | cut -f1))"

# Purge stale rows from the bundled demo DB. The dev workspace can
# accumulate pollution between builds:
#   (a) Export-clip rows pointing at demo/exports/... — the rsync
#       above EXCLUDES exports/ from the bundle so those rows
#       reference files that won't exist on the buyer's seeded
#       workspace. The lifespan path-rewrite in api/main.py skips
#       them (safety gate: target must exist), leaving them as
#       ghost entries that surface in /api/files listings and
#       confuse the buyer.
#   (b) /private/var/folders/.../pytest-NNN/... rows from pytest
#       runs that accidentally indexed test temp files into the
#       dev demo DB (observed as real pollution when the dev DB
#       was inspected).
#   (c) Anything else outside the canonical demo subdirs
#       (episodes/, real_videos/, real_photos/) is also suspect.
#
# DELETE cascades to transcript_segments / ocr_segments / keyframes
# via the schema's ON DELETE CASCADE (requires PRAGMA foreign_keys
# = ON per-connection — SQLite's default is OFF, which is what
# bit the earlier cleanup attempts that silently left
# orphans). VACUUM reclaims the freed pages.
#
# Idempotent: a clean DB matches none of the WHERE clauses and the
# DELETE is a no-op.
DEMO_DB="$RES/demo/db/tern.db"
if [ -f "$DEMO_DB" ]; then
  echo "  cleaning stale rows from bundled demo DB…"
  sqlite3 "$DEMO_DB" <<'SQL'
PRAGMA foreign_keys = ON;
BEGIN;
DELETE FROM files
WHERE path LIKE '%/exports/%'
   OR path LIKE '/private/var/folders/%'
   OR path LIKE '/tmp/%';
-- big_buck_bunny.mp4 is a silent animation included for the
-- visual-scene-search demo. Whisper hallucinates dozens of
-- "* Sounds of the game *" bracket-annotation segments on its
-- background music track — pure noise that pollutes result
-- lists when a buyer types common words ("sounds", "game",
-- "don't") against the demo and gets ghost hits pointing at a
-- video with no actual speech in it. The file itself stays in
-- the demo for visual search; just the bogus transcripts go.
-- ON DELETE CASCADE keeps transcript_fts in sync via the
-- trigger; FTS trigger runs as part of the same transaction.
DELETE FROM transcript_segments
WHERE file_id IN (
  SELECT id FROM files WHERE path LIKE '%big_buck_bunny%'
);
COMMIT;
VACUUM;
SQL
fi

# Belt-and-suspenders: nuke any iCloud-suffixed survivors anywhere in
# the bundle. A prepare_bundle.sh run from before the rsync excludes
# existed may have copied `* 2.*` files into resources/ — `rsync --delete
# --exclude=PAT` treats matching dest files as if they don't exist, so
# the orphan stays. This sweep runs AFTER the demo rsync (an earlier
# version ran it between the api/app and demo rsyncs, so demo dupes
# survived even after that fix). Single whole-tree sweep covers app/,
# api/, demo/, AND iCloud dupes inside the bundled site-packages (e.g.
# `tern/models 2.py`, observed in an installed /Applications/Tern.app
# before the excludes existed).
find "$RES" \( -name '* [0-9].*' -o -name '* [0-9]' \) -type f -delete 2>/dev/null || true

bold ""
bold "── Total bundle size ──"
du -sh "$RES"

bold ""
green "✓ resources/ is ready. Run \`cargo tauri build --bundles app dmg\`."
