#!/usr/bin/env bash
# Install the Developer ID certificate Apple issued, and prove it works.
#
#     scripts/install_signing_cert.sh ~/Downloads/developerID_application.cer
#
# The private key was generated locally beforehand and never left this Mac;
# Apple only ever saw the CSR. This pairs the certificate they signed back up
# with that key, so codesign can use it.

set -euo pipefail

CER="${1:?usage: install_signing_cert.sh <path-to-.cer>}"
KEYDIR="$HOME/.tern-signing"
KEY="$KEYDIR/devid.key"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }

[ -f "$CER" ] || { red "no such file: $CER"; exit 1; }
[ -f "$KEY" ] || {
  red "private key missing at $KEY"
  red "Without it the certificate is useless — Apple cannot re-issue the key,"
  red "only a new certificate for a new key. Generate a fresh CSR and start over."
  exit 1
}

bold "── 1. what Apple issued ──"
# .cer from the portal is DER. Read it without importing anything yet.
if ! openssl x509 -inform DER -in "$CER" -noout -subject -enddate 2>/dev/null; then
  # Some downloads arrive PEM-encoded.
  openssl x509 -in "$CER" -noout -subject -enddate \
    || { red "  cannot parse $CER as a certificate"; exit 1; }
fi

SUBJ="$(openssl x509 -inform DER -in "$CER" -noout -subject 2>/dev/null \
        || openssl x509 -in "$CER" -noout -subject)"
case "$SUBJ" in
  *"Developer ID Application"*) green "  correct type: Developer ID Application" ;;
  *"Apple Development"*|*"Apple Distribution"*)
    red "  this is a development/distribution certificate, not Developer ID."
    red "  Those cannot sign software for distribution outside the App Store."
    red "  Go back and pick 'Developer ID Application'."
    exit 1 ;;
  *) red "  unexpected certificate type:"; echo "    $SUBJ"
     red "  Continuing anyway, but check this is what you meant."; ;;
esac

bold "── 2. check the certificate matches our private key ──"
# If these moduli differ, the cert was issued against a different CSR and
# codesign will never find a usable identity.
CERT_MOD="$( { openssl x509 -inform DER -in "$CER" -noout -modulus 2>/dev/null \
            || openssl x509 -in "$CER" -noout -modulus; } | openssl md5)"
KEY_MOD="$(openssl rsa -in "$KEY" -noout -modulus 2>/dev/null | openssl md5)"
if [ "$CERT_MOD" != "$KEY_MOD" ]; then
  red "  ✗ certificate does not match $KEY"
  red "    It was issued against a different CSR. Find the matching key, or"
  red "    generate a new CSR from this key and request a new certificate."
  exit 1
fi
green "  matches the private key"

bold "── 3. import into the login keychain ──"
# -A would let ANY app use the key without prompting. Naming the tools that
# need it keeps the prompt-free access narrow.
security import "$KEY" -k "$HOME/Library/Keychains/login.keychain-db" \
  -T /usr/bin/codesign -T /usr/bin/security -T /usr/bin/productsign \
  2>&1 | grep -v "already exists" || true
security import "$CER" -k "$HOME/Library/Keychains/login.keychain-db" \
  -T /usr/bin/codesign -T /usr/bin/security \
  2>&1 | grep -v "already exists" || true
green "  imported"

bold "── 4. does codesign actually see an identity now? ──"
IDS="$(security find-identity -v -p codesigning 2>/dev/null || true)"
echo "$IDS" | sed 's/^/  /'
if ! echo "$IDS" | grep -q "Developer ID Application"; then
  red "  ✗ no Developer ID Application identity found."
  red "    Open Keychain Access, find the certificate, and check it has a"
  red "    private key nested under it. If not, the pairing failed."
  exit 1
fi

IDENTITY="$(echo "$IDS" | grep "Developer ID Application" | head -1 \
            | sed 's/.*"\(.*\)"/\1/')"
green "  usable identity: $IDENTITY"

bold "── 5. prove it can sign something ──"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cp /bin/echo "$TMP/probe"
if codesign --force --timestamp --options runtime \
     --sign "$IDENTITY" "$TMP/probe" 2>"$TMP/err"; then
  codesign -dv --verbose=2 "$TMP/probe" 2>&1 | grep -E "Authority|TeamIdentifier" | sed 's/^/  /'
  green "  signing works"
else
  red "  ✗ test signing failed:"
  sed 's/^/    /' "$TMP/err"
  red "    A keychain prompt may be waiting, or the key is not marked as"
  red "    allowing codesign. Check Keychain Access → the key → Access Control."
  exit 1
fi

printf '\n'
bold "next:"
cat <<EOF
  export APPLE_SIGNING_IDENTITY="$IDENTITY"

  # one-time, needs an app-specific password from account.apple.com.
  # APPLE_ID is the Apple ID of the developer account; TEAM_ID is the
  # 10-character team ID in brackets at the end of the identity above.
  xcrun notarytool store-credentials tern-notary \\
    --apple-id "\$APPLE_ID" --team-id "\$TEAM_ID"

  export APPLE_KEYCHAIN_PROFILE=tern-notary
  ./scripts/release.sh
EOF
