#!/usr/bin/env bash
# Run the wacli sync daemon and the Python MCP server side by side.
# On a fresh volume with PAIR_PHONE set, pair first (prints a code to the logs
# that you enter on your phone under Linked Devices > Link with phone number).
set -uo pipefail

STORE="${WACLI_STORE_DIR:-/data}"
mkdir -p "$STORE"

authed() { wacli auth status --json 2>/dev/null | grep -q '"authenticated":[[:space:]]*true'; }

# Start the MCP server immediately, independent of pairing: the HTTP endpoint is
# live right away; reads return data once the store fills, sends work once paired.
python main.py &
MCP_PID=$!
echo "[entrypoint] MCP server started (pid $MCP_PID)" >&2

# Stable pairing loop: keep re-minting a fresh phone-code (without crash-looping
# the whole container) until the device is linked. Gentle spacing avoids
# WhatsApp's pairing rate limit.
while ! authed; do
  if [ -z "${PAIR_PHONE:-}" ]; then
    echo "[entrypoint] not paired and PAIR_PHONE unset; pair via:" >&2
    echo "    fly ssh console -C 'wacli auth --qr-format text'" >&2
    sleep 30
    continue
  fi
  echo "[entrypoint] not paired — minting a fresh phone-code for ${PAIR_PHONE}." >&2
  wacli auth --phone "$PAIR_PHONE" --idle-exit 3m || true
  authed && break
  echo "[entrypoint] pairing window elapsed without completion; re-requesting shortly." >&2
  sleep 5
done
echo "[entrypoint] device paired ✓" >&2

# Keep the WhatsApp connection live + store fresh.
wacli sync --follow &
SYNC_PID=$!
echo "[entrypoint] wacli sync --follow started (pid $SYNC_PID)" >&2

term() { kill "$SYNC_PID" "$MCP_PID" 2>/dev/null || true; }
trap term TERM INT

# Exit (→ platform restart) as soon as either child exits.
wait -n
echo "[entrypoint] a child exited; shutting down" >&2
term
wait 2>/dev/null || true
exit 1
