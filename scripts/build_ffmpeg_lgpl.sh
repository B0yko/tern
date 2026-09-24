#!/usr/bin/env bash
# Build the ffmpeg that ships inside Tern, under LGPL 2.1.
#
# WHY THIS EXISTS
#
# Homebrew's ffmpeg is built with --enable-gpl, --enable-libx264 and
# --enable-libx265. Putting that binary inside a closed-source application
# makes the whole thing a GPL work, which would oblige us to hand Tern's
# source to every customer. This script produces an ffmpeg with no GPL
# component in it, so the bundle can be distributed under our own licence.
#
# It also satisfies the practical half of LGPL 2.1 section 6: the libraries
# are shared objects the user can replace with their own build, and this
# script is the recipe that made them.
#
# WHAT CHANGES AS A RESULT
#
# H.264 encoding moves from libx264 to Apple's VideoToolbox. Measured on an
# 8-second 1080p clip against a lossless reference:
#
#     x264 -crf 20 -preset fast    985 KB   49.63 dB   0.55 s
#     h264_videotoolbox -q:v 75   3254 KB   49.65 dB   1.01 s
#
# Same quality, roughly 3x the bytes, and no faster on short clips. See the
# comment in service_pipeline/tern/clip.py for why that trade is the right
# one for this product.
#
# LAME stays: it is LGPL (version 2 or later), not GPL, so MP3 export is
# unaffected.
#
# USAGE
#
#     scripts/build_ffmpeg_lgpl.sh [output-dir]
#
# Requires: nasm, pkg-config, lame  (brew install nasm pkg-config lame)

set -euo pipefail

FFMPEG_VERSION="8.0.1"
# Checksum of the tarball this build was verified against. Compare with the
# signature published on https://ffmpeg.org/download.html before bumping.
FFMPEG_SHA256="05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="${1:-$ROOT/third_party/ffmpeg/build}"
WORK="$OUT/work"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
red()  { printf '\033[31m%s\033[0m\n' "$1"; }

for tool in nasm pkg-config curl tar install_name_tool otool codesign; do
  command -v "$tool" >/dev/null || { red "missing: $tool"; exit 1; }
done

LAME_PREFIX="$(brew --prefix lame 2>/dev/null || echo /opt/homebrew/opt/lame)"
[ -f "$LAME_PREFIX/lib/libmp3lame.dylib" ] || {
  red "libmp3lame not found at $LAME_PREFIX — brew install lame"; exit 1;
}

mkdir -p "$WORK"
cd "$WORK"

bold "── 1. source ──"
TARBALL="ffmpeg-$FFMPEG_VERSION.tar.xz"
if [ ! -f "$TARBALL" ]; then
  curl -fSL --retry 3 -o "$TARBALL" \
    "https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VERSION.tar.xz"
fi
GOT_SHA="$(shasum -a 256 "$TARBALL" | cut -d' ' -f1)"
echo "  sha256: $GOT_SHA"
if [ "$GOT_SHA" != "$FFMPEG_SHA256" ]; then
  red "  ✗ checksum mismatch — expected $FFMPEG_SHA256"
  red "    Either the download is corrupt, or upstream re-rolled the release."
  red "    Verify against ffmpeg.org and update FFMPEG_SHA256 deliberately."
  exit 1
fi
rm -rf "ffmpeg-$FFMPEG_VERSION"
tar xf "$TARBALL"
cd "ffmpeg-$FFMPEG_VERSION"

bold "── 2. configure (LGPL 2.1, no GPL components) ──"
PKG_CONFIG_PATH="$LAME_PREFIX/lib/pkgconfig" ./configure \
  --prefix="$OUT/install" \
  --enable-shared --disable-static \
  --disable-gpl --disable-nonfree --disable-version3 \
  --disable-libx264 --disable-libx265 --disable-libxvid --disable-libvidstab \
  --disable-doc --disable-debug \
  --enable-videotoolbox --enable-audiotoolbox \
  --enable-libmp3lame \
  --extra-cflags="-I$LAME_PREFIX/include" \
  --extra-ldflags="-L$LAME_PREFIX/lib" \
  --enable-neon \
  | tail -3

bold "── 3. verify the licence before spending time on a build ──"
for flag in GPL NONFREE VERSION3; do
  if grep -q "define CONFIG_$flag 1" config.h; then
    red "  ✗ CONFIG_$flag is set — this build would NOT be LGPL"; exit 1
  fi
  echo "  CONFIG_$flag = 0"
done
for enc in LIBX264 LIBX265 LIBXVID; do
  if grep -qh "define CONFIG_${enc}_ENCODER 1" config*.h; then
    red "  ✗ $enc encoder present — GPL leaked in"; exit 1
  fi
done
echo "  no GPL encoders present"
for enc in H264_VIDEOTOOLBOX AAC LIBMP3LAME MJPEG; do
  grep -qh "define CONFIG_${enc}_ENCODER 1" config*.h \
    || { red "  ✗ $enc encoder missing — the app needs it"; exit 1; }
done
echo "  required encoders present"

bold "── 4. build ──"
make -j"$(sysctl -n hw.ncpu)" >/dev/null
make install >/dev/null

bold "── 5. stage as bin/ + libs/ ──"
STAGE="$OUT/stage"
rm -rf "$STAGE"; mkdir -p "$STAGE/bin" "$STAGE/libs"
cp "$OUT/install/bin/ffmpeg" "$OUT/install/bin/ffprobe" "$STAGE/bin/"
for f in "$OUT/install/lib"/*.dylib; do
  [ -L "$f" ] || cp "$f" "$STAGE/libs/"
done
cp "$LAME_PREFIX/lib/libmp3lame.0.dylib" "$STAGE/libs/"

# Rewrite absolute paths to @executable_path/../libs, matching the layout
# prepare_bundle.sh puts under Tern.app/Contents/Resources/resources/.
for f in "$STAGE/libs"/*.dylib; do
  install_name_tool -id "@executable_path/../libs/$(basename "$f")" "$f" 2>/dev/null
done
fix_refs() {
  local target="$1" prefix="$2" dep base cand
  otool -L "$target" | tail -n +2 | awk '{print $1}' | while read -r dep; do
    case "$dep" in /System/*|/usr/lib/*|@*) continue ;; esac
    base="$(basename "$dep")"
    if [ ! -f "$STAGE/libs/$base" ]; then
      for cand in "$STAGE/libs"/*.dylib; do
        case "$(basename "$cand")" in
          "${base%%.dylib}"*) base="$(basename "$cand")"; break ;;
        esac
      done
    fi
    [ -f "$STAGE/libs/$base" ] || continue
    install_name_tool -change "$dep" "$prefix/$base" "$target" 2>/dev/null
  done
}
for f in "$STAGE/libs"/*.dylib; do fix_refs "$f" "@loader_path"; done
for f in "$STAGE/bin"/*;        do fix_refs "$f" "@executable_path/../libs"; done
# Editing load commands invalidates the signature; macOS then refuses to load
# the arm64 binary at all. Re-sign ad-hoc. release.sh signs properly later.
for f in "$STAGE/libs"/*.dylib "$STAGE/bin"/*; do
  codesign --force --sign - "$f" >/dev/null 2>&1 || true
done

bold "── 6. prove it runs and carries no GPL ──"
"$STAGE/bin/ffmpeg" -hide_banner -version | head -1
if "$STAGE/bin/ffmpeg" -hide_banner -version | grep -q -- "--enable-gpl"; then
  red "  ✗ built binary still reports --enable-gpl"; exit 1
fi
"$STAGE/bin/ffmpeg" -hide_banner -encoders 2>/dev/null | grep -q libx264 \
  && { red "  ✗ libx264 present in the built binary"; exit 1; }
"$STAGE/bin/ffmpeg" -hide_banner -encoders 2>/dev/null | grep -q h264_videotoolbox \
  || { red "  ✗ h264_videotoolbox missing"; exit 1; }
echo "  licence: LGPL 2.1, no GPL encoders, VideoToolbox available"

bold "── 7. licence paperwork ──"
NOTICES="$OUT/../notices"
mkdir -p "$NOTICES"
cp COPYING.LGPLv2.1 "$NOTICES/ffmpeg-COPYING.LGPLv2.1"
cp "$WORK/$TARBALL" "$NOTICES/../$TARBALL" 2>/dev/null || true
echo "  wrote $NOTICES/ffmpeg-COPYING.LGPLv2.1"

printf '\n'
bold "done → $STAGE"
echo "Copy bin/ and libs/ into tauri/src-tauri/resources/ (prepare_bundle.sh does this)."
