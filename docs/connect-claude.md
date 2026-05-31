# Connect Claude

## Claude Code / Desktop (static token — no OAuth needed)
```sh
claude mcp add --transport http whatsapp \
  https://your-whatsapp-mcp.fly.dev/mcp \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
```
Works from anywhere your machine runs Claude Code, laptop or not.

## claude.ai (web — OAuth)
Requires the WorkOS OAuth setup in [`oauth-workos.md`](oauth-workos.md).

1. claude.ai → **Settings → Customize → Connectors** (or **+ → Add custom
   connector**).
2. **Name:** WhatsApp · **Remote MCP server URL:**
   `https://your-whatsapp-mcp.fly.dev/mcp` · leave OAuth Client ID/Secret blank
   (Dynamic Client Registration handles it).
3. Click **Add**, then **Connect** → you're redirected to WorkOS AuthKit → sign in
   with an allowlisted account → done.
