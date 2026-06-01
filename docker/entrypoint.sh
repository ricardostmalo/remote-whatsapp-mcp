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

# Sync watchdog. wacli #68: `sync --follow` can stay connected while the local
# store silently goes stale. Guard it — every CHECK_SECS, read doctor's
# last_sync_at; if it ages past STALE_SECS, bounce sync so it reconnects and
# catches up. Also restarts sync if the process dies.
STALE_SECS="${WACLI_STALE_SECS:-900}"
CHECK_SECS="${WACLI_CHECK_SECS:-300}"

sync_watchdog() {
  while true; do
    wacli sync --follow &
    local sp=$!
    echo "[watchdog] sync started (pid $sp)" >&2
    while kill -0 "$sp" 2>/dev/null; do
      sleep "$CHECK_SECS"
      local last last_epoch now age
      last=$(wacli doctor --json 2>/dev/null | sed -n 's/.*"last_sync_at":"\([^"]*\)".*/\1/p')
      [ -z "$last" ] && continue
      last_epoch=$(date -d "$last" +%s 2>/dev/null || echo 0)
      [ "$last_epoch" -eq 0 ] && continue
      now=$(date +%s); age=$((now - last_epoch))
      if [ "$age" -gt "$STALE_SECS" ]; then
        echo "[watchdog] store stale (${age}s > ${STALE_SECS}s) — bouncing sync" >&2
        kill "$sp" 2>/dev/null || true
        break
      fi
    done
    echo "[watchdog] sync ended; restarting in 5s" >&2
    sleep 5
  done
}

sync_watchdog &
SYNC_PID=$!
echo "[entrypoint] sync watchdog started (pid $SYNC_PID); stale threshold ${STALE_SECS}s" >&2

term() { kill "$SYNC_PID" "$MCP_PID" 2>/dev/null || true; }
trap term TERM INT

# The watchdog self-heals sync; only a dead MCP server should restart the machine.
wait "$MCP_PID"
echo "[entrypoint] MCP server exited; shutting down" >&2
term
exit 1
