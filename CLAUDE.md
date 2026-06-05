# CLAUDE.md

Guidance for AI agents (and humans) working on this repo. Read this before
touching how the MCP tools talk to `wacli`. (`AGENTS.md` is a symlink to this
file, so a tool looking for either name gets the same content.)

## What this is

A remote, always-on, OAuth-secured MCP server that fronts
[`openclaw/wacli`](https://github.com/openclaw/wacli) so Claude/ChatGPT can read
and act on a personal WhatsApp account from anywhere. Python MCP server in
`server/`, `wacli` (Go) engine built from source in the `Dockerfile`, deployed on
Fly.io with a persistent volume at `/data`.

## The one rule that matters: single-writer session

WhatsApp allows a linked device only one live session, and `wacli` enforces this
with a **store lock**. The always-on `wacli sync --follow` daemon holds that lock
**continuously**. Therefore:

- **Never spawn a second `wacli` process that needs the lock.** It will fail with
  `store is locked (... pid=...)`. A `wacli` that crashes mid-run can also orphan
  the lock and wedge the whole machine until restart. This is the single most
  important constraint in the project.
- The three tool families each avoid the lock a different way:
  - **Reads** (`list_*`, `search_*`, `get_*`) → open `wacli.db` as **read-only
    SQLite** (`?mode=ro`). No `wacli` process, no lock. See `_db_path()` /
    read helpers in `server/wacli.py`.
  - **Sends / reactions** (`send_*`, `react_to_message`, `mark_chat_read`,
    `request_history`) → shell out to `wacli`, which **delegates to the running
    daemon over a unix socket** (`<store>/.send.sock`) when the store is locked.
    This is built into wacli; just call the command normally.
  - **Media download / transcription** (`download_media`, `transcribe_audio`) →
    call `wacli media download --read-only --output <dir>`. Read-only mode runs
    `newApp(..., needLock=false, ...)` and fetches the encrypted blob **straight
    from WhatsApp's CDN** using the `DirectPath`/`MediaKey` already mirrored in
    the DB, then decrypts locally. No live connection, no lock.

If you add a tool, classify it into one of those three and follow the matching
pattern. Do not invent a fourth way that opens a writing `wacli`.

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
