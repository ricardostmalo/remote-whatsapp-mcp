# remote-whatsapp-mcp

Turn WhatsApp into a **remote, always-on, OAuth-secured MCP server** you can add
as a custom connector in **Claude** (Code / Desktop / claude.ai) and **ChatGPT** —
so an AI assistant can read and send your WhatsApp from anywhere, even with your
laptop off.

It wraps [`openclaw/wacli`](https://github.com/openclaw/wacli) (a mature Go
WhatsApp client built on `whatsmeow`) with a small Python MCP server and a WorkOS
AuthKit OAuth layer, deployed on Fly.io.

> ⚠️ **Unofficial API / use at your own risk.** wacli links your number as a
> WhatsApp Web "linked device" via the reverse-engineered `whatsmeow` protocol —
> this is **not** the official WhatsApp Business API and is against WhatsApp's
> Terms of Service. Your number can be banned. Don't use it to bulk-message, and
> prefer a number you can afford to lose. This is a personal-automation tool, not
> a product.

## How it works

```
  Claude / ChatGPT
        │  (custom connector, OAuth)
        ▼
  Fly.io machine (always-on)
   ├── Python MCP server  ──HTTP/OAuth──► you
   │     • streamable-HTTP at /mcp
   │     • WorkOS AuthKit OAuth (or static bearer token)
   │     • reads wacli.db (read-only), sends via the wacli CLI
   └── wacli sync --follow ──► holds the WhatsApp link, writes /data/wacli.db
```

- **Engine:** `wacli` keeps the WhatsApp connection alive and mirrors messages to
  SQLite on a persistent volume.
- **MCP layer:** `server/` exposes tools (search/list messages & chats, contacts,
  send text/file/voice, download media). Reads query `wacli.db` directly; sends
  shell out to `wacli send`.
- **Auth:** the `/mcp` endpoint accepts a WorkOS AuthKit OAuth token (what
  claude.ai and ChatGPT use) **or** a static bearer token (handy for Claude Code).
  Access is restricted to an email allowlist.

## Quick start

1. **Deploy to Fly + pair your number** → [`docs/deploy-fly.md`](docs/deploy-fly.md)
2. **Set up OAuth (WorkOS AuthKit)** so claude.ai/ChatGPT can connect →
   [`docs/oauth-workos.md`](docs/oauth-workos.md)
3. **Add the connector** in [Claude](docs/connect-claude.md) and
   [ChatGPT](docs/connect-chatgpt.md)

Copy `.env.example` → `.env` and fill it in; every secret is set via
`fly secrets set`, never committed.

## MCP tools

`search_contacts`, `get_contact`, `list_messages`, `list_chats`, `get_chat`,
`get_direct_chat_by_contact`, `get_contact_chats`, `get_last_interaction`,
`get_message_context`, `send_message`, `send_file`, `send_audio_message`,
`download_media`.

## Credits

Built on [`openclaw/wacli`](https://github.com/openclaw/wacli) (MIT). MCP layer +
WorkOS OAuth + Fly deployment in this repo are MIT-licensed.
