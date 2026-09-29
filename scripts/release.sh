#!/usr/bin/env bash
# Release pipeline: prep → build → codesign → notarize → staple → ship.
# Run once you have an Apple Developer cert + notarytool keychain profile.
#
# Required env vars:
#   APPLE_SIGNING_IDENTITY  e.g. "Developer ID Application: Andrii Boiko (TEAMID)"
#   APPLE_KEYCHAIN_PROFILE  notarytool credential alias (set up via:
#     xcrun notarytool store-credentials APPLE_KEYCHAIN_PROFILE \
#       --apple-id you@email.com --team-id TEAMID --password APP_SPECIFIC_PWD)
#
# Or invoke with --skip-signing for a dev build that just verifies the bundle.

set -uo pipefail
HERE="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ROOT="$(dirname "$HERE")"
APP_NAME="Tern.app"

# Derive VERSION from tauri.conf.json — single source of truth for the
# DMG filename Tauri emits. Hardcoded `Tern_0.1.0_aarch64.dmg` would break:
# the moment we bumped tauri.conf.json to 0.1.1 (or any
# future version), tauri-bundler wrote `Tern_0.1.1_aarch64.dmg` while
# release.sh kept looking for the 0.1.0 path → the `[ -f "$DMG_PATH" ]`
# guard at line ~40 errored out with "  .dmg not built — see cargo
# output" and the whole signed/notarized release pipeline aborted
# AFTER cargo tauri build had already burned ~10 minutes. Auto-derive
# so a version bump in conf.json just works.
VERSION=$(python3 -c "import json,sys; print(json.load(open('$ROOT/tauri/src-tauri/tauri.conf.json'))['version'])" 2>/dev/null) \
  || { printf '\033[31m  could not read version from tauri.conf.json — fix that file first\033[0m\n'; exit 1; }
DMG_NAME="Tern_${VERSION}_aarch64.dmg"
BUILD_DIR="$ROOT/tauri/src-tauri/target/release/bundle"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

SKIP_SIGNING=0
[ "${1:-}" = "--skip-signing" ] && SKIP_SIGNING=1

# Updater pubkey sanity check. tauri.conf.json ships with the literal
# placeholder "REPLACE_WITH_GENERATED_PUBKEY" so a fresh-clone contributor
# doesn't accidentally re-sign with a checked-in key. Catching this BEFORE
# `cargo tauri build` (which is ~10 minutes on a cold build) saves the
# user from a long round-trip just to discover the bundled .app would
# fail every auto-update signature verification at customer install time.
#
# Skipped on --skip-signing (dev sanity check that doesn't need the
# updater configured) so contributors hacking on UI code aren't blocked
# by a missing release-only secret.
if [ "$SKIP_SIGNING" != "1" ]; then
  PUBKEY=$(python3 -c "import json,sys; print(json.load(open('$ROOT/tauri/src-tauri/tauri.conf.json'))['plugins']['updater']['pubkey'])" 2>/dev/null) \
    || { red "  ✗ could not read updater pubkey from tauri.conf.json — fix that file first"; exit 1; }
  if [ "$PUBKEY" = "REPLACE_WITH_GENERATED_PUBKEY" ]; then
    red "  ✗ tauri.conf.json still has the placeholder updater pubkey."
    red "    Generate a real one and update plugins.updater.pubkey:"
    red "      cargo install tauri-cli --version '^2.0'"
    red "      cargo tauri signer generate -w ~/.tauri/tern.key"
    red "    Then put the PUBLIC half (printed to stdout) into the config and"
    red "    keep the PRIVATE half (~/.tauri/tern.key) for signing latest.json"
    red "    signatures. Without this, customers receive .dmg updates that"
    red "    fail signature verification → auto-update silently dies forever."
    red "    (Pass --skip-signing if you're building unsigned for local dev.)"
    exit 1
  fi
fi

bold "── 1. prepare_bundle.sh (resources/ population) ──"
"$HERE/prepare_bundle.sh" || { red "  prepare_bundle.sh failed"; exit 1; }

bold ""
bold "── 2. cargo tauri build (app + dmg) ──"
cd "$ROOT/tauri/src-tauri"
PATH="$HOME/.cargo/bin:/opt/homebrew/bin:$PATH" cargo tauri build --bundles app dmg 2>&1 | tail -8

APP_PATH="$BUILD_DIR/macos/$APP_NAME"
DMG_PATH="$BUILD_DIR/dmg/$DMG_NAME"

[ -d "$APP_PATH" ] || { red "  Tern.app not built — see cargo output"; exit 1; }
[ -f "$DMG_PATH" ] || { red "  .dmg not built — see cargo output"; exit 1; }
green "  $APP_PATH  ($(du -sh $APP_PATH | cut -f1))"
green "  $DMG_PATH  ($(du -sh $DMG_PATH | cut -f1))"

if [ "$SKIP_SIGNING" = "1" ]; then
  bold ""
  green "✓ unsigned bundle ready. Copy to dist/ manually if needed."
  exit 0
fi

# ── Signing requirements check ────────────────────────────────────────────
: "${APPLE_SIGNING_IDENTITY:?ERROR: set APPLE_SIGNING_IDENTITY to your Developer ID Application string}"
: "${APPLE_KEYCHAIN_PROFILE:?ERROR: set APPLE_KEYCHAIN_PROFILE for notarytool (see top of script)}"

bold ""
bold "── 3. Code-sign every executable + dylib inside .app ──"
# Sign all dylibs first, then frameworks, then executables, then the .app itself.
# --options runtime enables Hardened Runtime (required for notarization).
# --timestamp embeds a trusted timestamp (also required).
find "$APP_PATH" \( -name "*.dylib" -o -name "*.so" \) -type f | while read -r lib; do
  codesign --force --options runtime --timestamp \
    --sign "$APPLE_SIGNING_IDENTITY" "$lib" 2>&1 | tail -1
done
# Sign the bundled helper binaries
for bin in "$APP_PATH/Contents/Resources/resources/bin/"*; do
  codesign --force --options runtime --timestamp \
    --sign "$APPLE_SIGNING_IDENTITY" "$bin" 2>&1 | tail -1
done
# Sign the bundled Python interpreter
if [ -d "$APP_PATH/Contents/Resources/resources/python" ]; then
  find "$APP_PATH/Contents/Resources/resources/python" -type f \( -perm -u+x -o -name "*.dylib" \) | while read -r f; do
    codesign --force --options runtime --timestamp \
      --sign "$APPLE_SIGNING_IDENTITY" "$f" 2>&1 | tail -1
  done
fi
# Finally sign the .app
codesign --force --options runtime --timestamp --deep \
  --sign "$APPLE_SIGNING_IDENTITY" "$APP_PATH"
green "  .app signed"

# Re-create .dmg (it has the OLD unsigned .app inside)
bold ""
bold "── 4. Re-create .dmg with signed .app ──"
rm -f "$DMG_PATH"
cd "$ROOT/tauri/src-tauri"
PATH="$HOME/.cargo/bin:/opt/homebrew/bin:$PATH" cargo tauri build --bundles dmg 2>&1 | tail -3
codesign --force --options runtime --timestamp \
  --sign "$APPLE_SIGNING_IDENTITY" "$DMG_PATH"
green "  .dmg signed ($(du -sh $DMG_PATH | cut -f1))"

# ── 5. Notarize ────────────────────────────────────────────────────────────
bold ""
bold "── 5. Notarize via notarytool (waits for Apple to process) ──"
xcrun notarytool submit "$DMG_PATH" \
  --keychain-profile "$APPLE_KEYCHAIN_PROFILE" \
  --wait \
  --timeout 30m \
  --output-format json | tee /tmp/notarize.json | python3 -m json.tool
NOTARY_STATUS=$(python3 -c "import json; print(json.load(open('/tmp/notarize.json')).get('status'))")
[ "$NOTARY_STATUS" = "Accepted" ] || { red "  notarization status: $NOTARY_STATUS — abort"; exit 1; }
green "  Apple accepted the notarization"

# ── 6. Staple the ticket ──────────────────────────────────────────────────
bold ""
bold "── 6. Staple notarization ticket to the .dmg ──"
xcrun stapler staple "$DMG_PATH"
xcrun stapler validate "$DMG_PATH"

# ── 7. Copy to dist/ ──────────────────────────────────────────────────────
mkdir -p "$ROOT/../dist"
rm -rf "$ROOT/../dist/$APP_NAME"
cp -R "$APP_PATH" "$ROOT/../dist/$APP_NAME"
cp "$DMG_PATH" "$ROOT/../dist/$DMG_NAME"

# ── 8. Verify signed+stapled bundle — fail loudly on broken release ──────
# Previously the script just printed "Verify: spctl ..." for the user to run.
# That gate was easy to skip — and a release that fails spctl on the customer's
# Mac shows up as Gatekeeper blocking ("Tern.app cannot be opened") AFTER they
# downloaded the 900MB .dmg. Run the checks here so we fail in this script
# rather than at customer install time.
bold ""
bold "── 8. Verify signature + Gatekeeper acceptance ──"
DIST_DMG="$ROOT/../dist/$DMG_NAME"
DIST_APP="$ROOT/../dist/$APP_NAME"

if ! codesign --verify --deep --strict --verbose=2 "$DIST_APP" 2>&1 | tail -3; then
  red "  codesign --verify FAILED — .app signature is broken"
  exit 1
fi
green "  codesign --verify ✓"

if ! spctl -a -t open --context context:primary-signature "$DIST_DMG" 2>&1 | tail -3; then
  red "  spctl FAILED — Gatekeeper would block this .dmg on customer Macs"
  exit 1
fi
green "  spctl Gatekeeper accept ✓"

if ! xcrun stapler validate "$DIST_DMG" 2>&1 | tail -2; then
  red "  stapler validate FAILED — notarization ticket not embedded"
  exit 1
fi
green "  stapler validate ✓"

# ── 9. Emit SHA256 + size for release notes / latest.json ────────────────
bold ""
bold "── 9. Release artifacts ──"
DMG_SHA=$(shasum -a 256 "$DIST_DMG" | awk '{print $1}')
DMG_BYTES=$(stat -f %z "$DIST_DMG")
DMG_HUMAN=$(du -sh "$DIST_DMG" | cut -f1)

cat <<EOF
  .dmg:    $DIST_DMG
  Size:    $DMG_HUMAN ($DMG_BYTES bytes)
  SHA256:  $DMG_SHA

  Paste this into the GitHub release notes and
  the SHA256 field of latest.json for the auto-updater:

    "url": "https://github.com/B0yko/tern/releases/download/vX.Y.Z/$DMG_NAME",
    "signature": "<paste tauri-signer .sig file contents here>",
    "sha256": "$DMG_SHA"
EOF

bold ""
green "✓ Signed + notarized + verified .dmg in dist/$DMG_NAME"
