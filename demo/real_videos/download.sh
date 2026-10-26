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
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ERROR: ffmpeg not installed. Run: brew install ffmpeg"
  exit 1
fi

# YouTube serves 720p as separate video and audio streams, so the old
# "best single file" selector found nothing. Ask for H.264 video (what the
# keyframe extractor and Apple Vision handle everywhere) plus AAC audio, up
# to 720p, and let ffmpeg merge them into one mp4.
FORMAT='bv*[vcodec^=avc1][height<=720]+ba[ext=m4a]/bv*[ext=mp4][height<=720]+ba/b[height<=720]/b'

# 1. Big Buck Bunny (CC-BY, Blender Foundation), full 10:34
if [ ! -f big_buck_bunny.mp4 ]; then
  echo "→ Big Buck Bunny (CC-BY, Blender Foundation)..."
  yt-dlp \
    -f "$FORMAT" \
    --merge-output-format mp4 \
    -o 'big_buck_bunny.%(ext)s' \
    --no-warnings \
    "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
else
  echo "✓ big_buck_bunny.mp4 already present"
fi

# 2. YC Lecture 1 "How to Start a Startup" — first 10:00 only. The uploader's
#    description names CC BY-NC-ND 2.5; fetched for local testing, not redistributed.
#    --download-sections would hand the transfer to ffmpeg, which YouTube serves
#    at playback speed (twenty minutes for ten), so fetch the whole lecture with
#    yt-dlp's own downloader and cut the first ten minutes locally.
if [ ! -f yc_lecture1_intro.mp4 ]; then
  echo "→ YC Lecture 1 first 10 min (Y Combinator, local testing only)..."
  rm -f yc_lecture1_full.mp4 yc_lecture1_intro.part.mp4
  yt-dlp \
    -f "$FORMAT" \
    --merge-output-format mp4 \
    -o 'yc_lecture1_full.%(ext)s' \
    --no-warnings \
    "https://www.youtube.com/watch?v=CBYhVcO4WgI"
  # Cut into a temporary name and rename only on success, so an interrupted
  # cut cannot leave a short file that the next run reports as "present".
  ffmpeg -y -loglevel error -i yc_lecture1_full.mp4 -t 600 -c copy yc_lecture1_intro.part.mp4
  mv yc_lecture1_intro.part.mp4 yc_lecture1_intro.mp4
  rm -f yc_lecture1_full.mp4
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
