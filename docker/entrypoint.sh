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

# Sync supervisor. Keep `sync --follow` running and restart it only if the
# process actually exits. We deliberately do NOT poll doctor's `last_sync_at` as
# a "staleness" signal: wacli derives that field from the last *message*
# timestamp, not connection health (cmd/wacli/doctor.go), so a normal quiet
# period (>15 min with no incoming messages) read as "stale" and bounced sync —
# dropping the live WhatsApp session and flapping the claude.ai connector. A
# sidecar can't observe the daemon's socket anyway (the daemon holds the lock),
# so true silent-stale recovery belongs in wacli's own reconnect logic, not here.
sync_watchdog() {
  while true; do
    wacli sync --follow &
    local sp=$!
    echo "[watchdog] sync started (pid $sp)" >&2
    wait "$sp"
    echo "[watchdog] sync exited; restarting in 5s" >&2
    sleep 5
  done
}

sync_watchdog &
SYNC_PID=$!
echo "[entrypoint] sync supervisor started (pid $SYNC_PID)" >&2

term() { kill "$SYNC_PID" "$MCP_PID" 2>/dev/null || true; }
trap term TERM INT

# The watchdog self-heals sync; only a dead MCP server should restart the machine.
wait "$MCP_PID"
echo "[entrypoint] MCP server exited; shutting down" >&2
term
exit 1
