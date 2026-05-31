# OAuth with WorkOS AuthKit

claude.ai and ChatGPT custom connectors authenticate over **OAuth**, not a static
header — so to use those, put WorkOS AuthKit in front of the server as the
authorization server. (Claude Code/Desktop can use the static `MCP_AUTH_TOKEN`
without this.) WorkOS AuthKit's free tier covers a single user.

## 1. WorkOS dashboard
1. Create a free WorkOS account → note your **AuthKit domain** under
   **Domains** (looks like `https://your-env.authkit.app`).
2. **Connect → Configuration → MCP Auth → Enable**: turn on **Dynamic Client
   Registration** *and* **Client ID Metadata Document** (so Claude/ChatGPT can
   self-register).
3. **Connect → Configuration → MCP resource indicators → Edit**: add your MCP URL
   `https://your-whatsapp-mcp.fly.dev/mcp` as a valid resource indicator.

## 2. Point the server at AuthKit
```sh
fly secrets set \
  AUTHKIT_DOMAIN="https://your-env.authkit.app" \
  MCP_ALLOWED_EMAILS="you@example.com" \
  --app your-whatsapp-mcp
fly deploy --app your-whatsapp-mcp
```
`MCP_ALLOWED_EMAILS` restricts access to your account(s). With `AUTHKIT_DOMAIN`
set, the server advertises `/.well-known/oauth-protected-resource` and returns a
`401` + `WWW-Authenticate` challenge that kicks off the OAuth flow.

## 3. Verify discovery
```sh
curl -s https://your-whatsapp-mcp.fly.dev/.well-known/oauth-protected-resource/mcp
# → {"resource":".../mcp","authorization_servers":["https://your-env.authkit.app"],...}
```

## How auth resolves on the server
`server/main.py` validates each bearer token as **either** the static
`MCP_AUTH_TOKEN` **or** a WorkOS AuthKit JWT (signature checked against
`<authkit_domain>/oauth2/jwks`, issuer verified, optional email allowlist).
