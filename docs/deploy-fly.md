# Deploy to Fly.io + pair your number

## Prerequisites
- [flyctl](https://fly.io/docs/flyctl/install/) and a Fly account **with a payment
  method added** (trial machines stop after 5 minutes — useless for always-on).
- Your phone, to pair the WhatsApp linked device.

## 1. App + volume
```sh
cp fly.toml.example fly.toml          # then edit `app` (globally unique) and region
fly apps create your-whatsapp-mcp
fly volumes create wa_data --region <region> --size 1 --app your-whatsapp-mcp
```

## 2. Secrets
```sh
fly secrets set \
  MCP_AUTH_TOKEN="$(openssl rand -hex 32)" \
  MCP_RESOURCE_URL="https://your-whatsapp-mcp.fly.dev/mcp" \
  PAIR_PHONE="+15551234567" \
  --app your-whatsapp-mcp
```
- `MCP_AUTH_TOKEN` — static bearer for Claude Code/Desktop. Save it.
- `PAIR_PHONE` — your number; the container uses it for phone-code pairing.
- (OAuth secrets `AUTHKIT_DOMAIN` / `MCP_ALLOWED_EMAILS` come later — see
  `oauth-workos.md`.)

## 3. Deploy
```sh
fly deploy --app your-whatsapp-mcp
```
Fly builds the image on its remote builder (no local Docker needed).

## 4. Pair (phone-code)
On first boot the container runs `wacli auth --phone "$PAIR_PHONE"` and prints an
8-character linking code to the logs:
```sh
fly logs --app your-whatsapp-mcp        # watch for "enter it on your phone"
```
On your phone: **WhatsApp → Settings → Linked Devices → Link a Device → Link with
phone number instead**, then enter the code. History then syncs for a few minutes.

> Prefer QR? Unset `PAIR_PHONE` and run
> `fly ssh console -C 'wacli auth --qr-format text'` and render the payload.

## 5. Verify
```sh
curl -s -o /dev/null -w "%{http_code}\n" https://your-whatsapp-mcp.fly.dev/mcp   # 401 (auth required) = good
curl -s -X POST https://your-whatsapp-mcp.fly.dev/mcp \
  -H "Authorization: Bearer <MCP_AUTH_TOKEN>" \
  -H "Accept: application/json, text/event-stream" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
# → a JSON-RPC result with serverInfo "whatsapp"
```
