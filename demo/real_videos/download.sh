#!/bin/bash
# Download the two demo videos for Tern's video-pipeline test, for local testing
# only (see README.md for their licences).
# Requires: yt-dlp (brew install yt-dlp) and ffmpeg.

set -euo pipefail
cd "$(dirname "$0")"

if ! command -v yt-dlp >/dev/null 2>&1; then
  echo "ERROR: yt-dlp not installed. Run: brew install yt-dlp"
  exit 1
fi

# 1. Big Buck Bunny (CC-BY, Blender Foundation), full 10:34
if [ ! -f big_buck_bunny.mp4 ]; then
  echo "→ Big Buck Bunny (CC-BY, Blender Foundation)..."
  yt-dlp \
    -f 'best[ext=mp4][height<=720]/best[height<=720]/best' \
    --merge-output-format mp4 \
    -o 'big_buck_bunny.%(ext)s' \
    --no-warnings \
    "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
else
  echo "✓ big_buck_bunny.mp4 already present"
fi

# 2. YC Lecture 1 "How to Start a Startup" — first 10:00 only. The uploader's
#    description names CC BY-NC-ND 2.5; fetched for local testing, not redistributed.
if [ ! -f yc_lecture1_intro.mp4 ]; then
  echo "→ YC Lecture 1 first 10 min (Y Combinator, local testing only)..."
  yt-dlp \
    -f 'best[ext=mp4][height<=720]/best[height<=720]/best' \
    --merge-output-format mp4 \
    --download-sections '*0:00-10:00' \
    --force-keyframes-at-cuts \
    -o 'yc_lecture1_intro.%(ext)s' \
    --no-warnings \
    "https://www.youtube.com/watch?v=CBYhVcO4WgI"
else
  echo "✓ yc_lecture1_intro.mp4 already present"
fi

echo ""
echo "Done. Files:"
ls -lh *.mp4

cat <<'EOF'

Next: index into the demo workspace. From the repository root, with ./run.sh
stopped (ChromaDB allows one process per workspace):
  cd service_pipeline
  uv run python -m tern.cli -w ../demo index ../demo/real_videos -l en
Or run scripts/init_demo.sh to index everything under demo/ through the API.

See README.md for verified queries to try.
EOF
