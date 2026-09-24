#!/usr/bin/env bash
# Prove the deployed licence endpoint actually works.
#
#   ./verify_deployment.sh <endpoint-url> <licence-key>
#
# Exercises the four cases that matter: a good key takes a seat, the same
# machine re-activates without taking another, an unknown key is refused,
# and a missing machine id is refused. Run it once after deploying and
# once after any change to decide.ts.

set -euo pipefail

ENDPOINT="${1:?usage: verify_deployment.sh <endpoint-url> <licence-key>}"
KEY="${2:?usage: verify_deployment.sh <endpoint-url> <licence-key>}"

green() { printf '\033[32m  ✓ %s\033[0m\n' "$1"; }
red()   { printf '\033[31m  ✗ %s\033[0m\n' "$1"; }

fails=0

call() {
  curl -sS --max-time 20 -X POST "$ENDPOINT" \
    -H 'Content-Type: application/json' -d "$1"
}

field() { python3 -c "import json,sys; print(json.load(sys.stdin).get('$1'))"; }

echo "endpoint: $ENDPOINT"
echo

# 1. a good key on a fresh machine
body=$(call "{\"license_key\":\"$KEY\",\"app_version\":\"0.1.0\",\"machine_id\":\"verify-machine-a\"}")
if [ "$(echo "$body" | field is_valid)" = "True" ]; then
  green "valid key accepted (seats_used=$(echo "$body" | field seats_used))"
else
  red "valid key refused: $body"; fails=$((fails+1))
fi

# 2. same machine again — must not consume a second seat
before=$(echo "$body" | field seats_used)
body=$(call "{\"license_key\":\"$KEY\",\"app_version\":\"0.1.0\",\"machine_id\":\"verify-machine-a\"}")
after=$(echo "$body" | field seats_used)
if [ "$(echo "$body" | field is_valid)" = "True" ] && [ "$before" = "$after" ]; then
  green "re-activation on a known machine is free (seats_used stayed $after)"
else
  red "re-activation changed the seat count: $before -> $after"; fails=$((fails+1))
fi

# 3. an unknown key
body=$(call '{"license_key":"TERN-0000-0000-0000","app_version":"0.1.0","machine_id":"verify-machine-a"}')
if [ "$(echo "$body" | field is_valid)" = "False" ]; then
  green "unknown key refused"
else
  red "unknown key accepted: $body"; fails=$((fails+1))
fi

# 4. no machine id — must not hand out a licence to something uncountable
body=$(call "{\"license_key\":\"$KEY\",\"app_version\":\"0.1.0\",\"machine_id\":\"\"}")
if [ "$(echo "$body" | field is_valid)" = "False" ]; then
  green "missing machine id refused"
else
  red "missing machine id accepted: $body"; fails=$((fails+1))
fi

echo
if [ "$fails" -eq 0 ]; then
  printf '\033[32mall four checks passed\033[0m\n'
  echo
  echo "Clean up the seats this script registered:"
  echo "  delete from licensing.license_machines where machine_id like 'verify-machine-%';"
else
  printf '\033[31m%s check(s) failed\033[0m\n' "$fails"
  exit 1
fi
