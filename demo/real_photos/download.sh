#!/bin/bash
# Fetch the 7 demo photos (Wikimedia Commons and Lorem Picsum) so the workspace
# can be re-indexed end-to-end. Requires: curl.

set -euo pipefail
cd "$(dirname "$0")"

UA="tern-demo-seed/1.0 (local demo workspace; https://github.com/B0yko/tern/issues)"

# Wikimedia CC-BY-SA cat (canonical, no thumb URL because the size suffixes change).
if [ ! -s cat_portrait.jpg ]; then
  echo "→ cat_portrait.jpg (Wikimedia, CC-BY-SA)"
  curl -fsSL -H "User-Agent: $UA" \
    -o cat_portrait.jpg \
    "https://upload.wikimedia.org/wikipedia/commons/3/3a/Cat03.jpg"
fi

# Wikimedia public-domain transparency-demo PNG.
if [ ! -s PNG_transparency_demonstration_1.png ]; then
  echo "→ PNG_transparency_demonstration_1.png (Wikimedia, PD)"
  curl -fsSL -H "User-Agent: $UA" \
    -o PNG_transparency_demonstration_1.png \
    "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png"
fi

# Lorem Picsum (photos from Unsplash, Unsplash License). Seeded → reproducible.
for seed in beach mountain office cafe whiteboard; do
  out="picsum_${seed}.jpg"
  if [ ! -s "$out" ]; then
    echo "→ $out (Lorem Picsum, Unsplash License)"
    curl -fsSL -o "$out" "https://picsum.photos/seed/${seed}/1280/853"
  fi
done

echo ""
echo "Done. Files:"
ls -lh *.jpg *.png 2>/dev/null

cat <<'EOF'

Next: index into the demo workspace. From the repository root, with ./run.sh
stopped (ChromaDB allows one process per workspace):
  cd service_pipeline
  uv run python -m tern.cli -w ../demo index ../demo/real_photos -l en
Or run scripts/init_demo.sh to index everything under demo/ through the API.

See README.md for queries that prove the photo pipeline works.
EOF
