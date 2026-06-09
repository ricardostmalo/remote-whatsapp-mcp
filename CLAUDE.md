# CLAUDE.md

Guidance for AI agents (and humans) working on this repo. Read this before
touching how the MCP tools talk to `wacli`. (`AGENTS.md` is a symlink to this
file, so a tool looking for either name gets the same content.)

## Communication style (Ricardo)

Be **very concise**. Lead with the answer; cut preamble, recap, and
self-narration. Short list over prose. No play-by-play. Surface only what's
decision-relevant; offer detail on request instead of dumping it. If a long
answer seems needed, give the 3-line version first and ask before expanding.

## What this is

A remote, always-on, OAuth-secured MCP server that fronts
[`openclaw/wacli`](https://github.com/openclaw/wacli) so Claude/ChatGPT can read
and act on a personal WhatsApp account from anywhere. Python MCP server in
`server/`, `wacli` (Go) engine built from source in the `Dockerfile`, deployed on
Fly.io with a persistent volume at `/data`.

## The one rule that matters: single-writer store lock

A wacli store can be driven by only **one wacli process at a time** — wacli guards
it with a local `flock` lockfile (`<store>/LOCK`, holding the owner pid). The
always-on `wacli sync --follow` daemon holds that lock **continuously**. (It's a
local file lock, **not** a WhatsApp limit — WhatsApp itself is multi-device; the
lock just stops two wacli processes from racing the same store.) Therefore:

- **Never spawn a second `wacli` process that needs the lock.** It fails with
  `store is locked (... pid=...)`. This is the single most important constraint.
- What works, and how each avoids the lock:
  - **Reads** (`list_*`, `search_*`, `get_*`) → call wacli's own read commands
    (`messages list/search/starred`, `chats list/show`, `contacts search/show`,
    `calls list`). They're `--json` and lock-free (`needLock=false`), so they run
    fine alongside the daemon, and we stay on wacli's stable CLI contract instead
    of its internal DB schema. Three reads have no clean command and use read-only
    SQLite instead: `get_contact_chats`, `get_group_info`, `get_message_context`.
    See `server/wacli.py`.
  - **Sends / reactions** (`send_*`, `react_to_message`) → shell out to `wacli`,
    which **delegates to the running daemon over a unix socket**
    (`<store>/.send.sock`) when the store is locked. Built into wacli for the
    send-type commands; just call the command normally.
  - **Media download / transcription** (`download_media`, `transcribe_audio`) →
    call `wacli media download --read-only --output <dir>`. Read-only mode runs
    `newApp(..., needLock=false, ...)` and fetches the encrypted blob **straight
    from WhatsApp's CDN** using the `DirectPath`/`MediaKey` already mirrored in
    the DB, then decrypts locally. No live connection, no lock.

- **Removed — `mark_chat_read` (`chats mark-read`) and `request_history`
  (`history backfill`).** Both need the live connection (`newApp(..., needLock=true,
  ...)`) and wacli does **not** delegate them over the socket (no case in
  `executeDelegatedSend`), so they fail with `store is locked` under the daemon.
  They were exposed as tools and removed. Don't re-add them without first patching
  wacli to delegate them over `.send.sock` (the `react` pattern), via upstream PR
  or a fork the Dockerfile builds from.

If you add a tool, classify it into reads / delegated-sends / read-only-download.
Anything that needs the live connection but isn't a delegated send will collide
with the daemon — see the unsupported pair above before exposing it as a tool.

### Known edge

Read-only CDN download relies on the message's `DirectPath`, which WhatsApp
expires after a while. Recent media (the realistic transcription case) works;
very old media may 404. The only recovery is a live-connection re-fetch, which
needs the lock — out of scope for the read-only path on purpose.

## Layout

- `server/main.py` — FastMCP server, transport + OAuth wiring, one `@mcp.tool()`
  per tool. Tools are thin; they call `server/wacli.py`.
- `server/wacli.py` — the backend. Read path (read-only SQLite) and write path
  (shell out to the `wacli` binary). This is where the lock rules above live.
- `docker/entrypoint.sh` — runs the MCP server and the `sync --follow` daemon
  side by side, with a stable phone-code pairing loop and a freshness watchdog
  that bounces sync if the store goes stale.
- `Dockerfile` — builds `wacli` from source (CGO + `sqlite_fts5`) and the Python
  runtime; `ffmpeg` is present for audio handling.
- `docs/` — deploy (Fly), OAuth (WorkOS AuthKit), and connector setup guides.

## Run & deploy

- **Local (stdio):** `cd server && MCP_TRANSPORT=stdio python main.py` (needs a
  `wacli` binary + a paired store; see `docs/`).
- **Deploy:** `fly deploy` from the repo root. The image rebuilds `wacli` and the
  server; `server/` changes redeploy quickly. Verify with `fly logs` and by
  calling a tool through the live connector.
- **Pair a fresh machine:** set `PAIR_PHONE`; the entrypoint mints a phone code in
  the logs that you enter under WhatsApp → Linked Devices.

## Secrets

Never commit secrets. All runtime config is set via `fly secrets set` and
documented as placeholders in `.env.example`. `fly.toml` is gitignored (app-name /
volume specifics); the repo ships `fly.toml.example`. Keep this personal bridge's
secrets separate from any product/app secrets.

## Verifying a change to media/transcription

Probe read-only download directly on the box — it's safe (no lock):

```
fly ssh console -C "wacli media download --chat <jid> --id <msgid> --output /tmp/t --read-only --json"
```

Then exercise the live tool through the connector. A green path returns
`"read_only": true` and a file under `--output`.
