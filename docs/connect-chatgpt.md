# Connect ChatGPT

Requires the WorkOS OAuth setup in [`oauth-workos.md`](oauth-workos.md). Custom
MCP connectors need a paid ChatGPT plan with **Developer mode** (Settings → Apps &
Connectors → Advanced → Developer mode).

1. ChatGPT → **Settings → Apps & Connectors → Advanced settings → Create app**
   (developer mode).
2. **Name:** WhatsApp · **Server URL:** `https://your-whatsapp-mcp.fly.dev/mcp` ·
   **Authentication:** OAuth (auto-discovered from the server) · tick *"I
   understand and want to continue"* · **Create**.
3. Click **Sign in with WhatsApp** → authorize at WorkOS AuthKit with an
   allowlisted account → **Allow access**. Connected.

> The server exposes custom tools (not just `search`/`fetch`), so Developer-mode
> connectors are required — the Deep Research connector slot won't accept it.
