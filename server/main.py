"""Remote WhatsApp MCP server (wacli engine + WorkOS OAuth).

- Reads/sends go through `wacli.py` (read-only SQLite + the `wacli` CLI).
- Transport: stdio by default; set MCP_TRANSPORT=http for remote hosting.
- Auth (HTTP mode): accepts EITHER the static MCP_AUTH_TOKEN (Claude Code/Desktop)
  OR a WorkOS AuthKit JWT (claude.ai / ChatGPT connectors). When AUTHKIT_DOMAIN
  is set, the MCP SDK serves /.well-known/oauth-protected-resource and enforces
  bearer auth via the verifier below.
"""

from __future__ import annotations

import hmac
import os
import signal
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

import wacli

_RESOURCE_URL = os.getenv("MCP_RESOURCE_URL", "http://localhost:8081/mcp")
_SDK_AUTH = False

if os.getenv("MCP_TRANSPORT", "stdio").strip().lower() in ("http", "streamable-http", "streamable_http"):
    from mcp.server.transport_security import TransportSecuritySettings

    _ts = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    _host = os.getenv("MCP_HOST", "0.0.0.0")
    _port = int(os.getenv("MCP_PORT", "8081"))
    _authkit = os.getenv("AUTHKIT_DOMAIN", "").strip().rstrip("/")

    if _authkit:
        import jwt as _jwt
        from mcp.server.auth.provider import AccessToken, TokenVerifier
        from mcp.server.auth.settings import AuthSettings

        class _CombinedVerifier(TokenVerifier):
            def __init__(self, static_token: str, issuer: str, resource_url: str):
                self._static = static_token
                self._issuer = issuer
                self._resource = resource_url
                self._jwks = _jwt.PyJWKClient(f"{issuer}/oauth2/jwks")
                allowed = os.getenv("MCP_ALLOWED_EMAILS", "").strip()
                self._allowed = {e.strip().lower() for e in allowed.split(",") if e.strip()}

            async def verify_token(self, token: str) -> "AccessToken | None":
                if self._static and hmac.compare_digest(token, self._static):
                    return AccessToken(token=token, client_id="static", scopes=[], expires_at=None, resource=self._resource)
                try:
                    key = self._jwks.get_signing_key_from_jwt(token).key
                    claims = _jwt.decode(token, key, algorithms=["RS256"], issuer=self._issuer, options={"verify_aud": False})
                except Exception:
                    return None
                if self._allowed:
                    email = str(claims.get("email", "")).lower()
                    if email and email not in self._allowed:
                        return None
                raw = claims.get("scope", "")
                scopes = raw.split() if isinstance(raw, str) else list(raw or [])
                return AccessToken(
                    token=token,
                    client_id=claims.get("client_id") or claims.get("azp") or claims.get("sub", "workos"),
                    scopes=scopes,
                    expires_at=claims.get("exp"),
                    resource=self._resource,
                )

        mcp = FastMCP(
            "whatsapp",
            host=_host,
            port=_port,
            transport_security=_ts,
            token_verifier=_CombinedVerifier(os.getenv("MCP_AUTH_TOKEN", "").strip(), _authkit, _RESOURCE_URL),
            auth=AuthSettings(issuer_url=_authkit, resource_server_url=_RESOURCE_URL, required_scopes=None),
        )
        _SDK_AUTH = True
    else:
        mcp = FastMCP("whatsapp", host=_host, port=_port, transport_security=_ts)
else:
    mcp = FastMCP("whatsapp")


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def search_contacts(query: str) -> list[dict[str, Any]]:
    """Search WhatsApp contacts by name or phone number."""
    return wacli.search_contacts(query)


@mcp.tool()
def get_contact(identifier: str) -> dict[str, Any]:
    """Look up a WhatsApp contact by phone number or JID."""
    return wacli.get_contact(identifier)


@mcp.tool()
def list_messages(
    chat_jid: str | None = None,
    sender_phone_number: str | None = None,
    query: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 50,
    from_me: bool | None = None,
    sort_by: str = "newest",
) -> list[dict[str, Any]]:
    """List/search WhatsApp messages. Dates are ISO-8601 ('2026-01-01'). sort_by: 'newest' or 'oldest'."""
    sender_jid = None
    if sender_phone_number:
        digits = "".join(c for c in sender_phone_number if c.isdigit())
        sender_jid = f"{digits}@s.whatsapp.net" if digits else sender_phone_number
    return wacli.list_messages(
        chat_jid=chat_jid, sender_jid=sender_jid, query=query, limit=limit,
        after=after, before=before, from_me=from_me, sort=sort_by,
    )


@mcp.tool()
def list_chats(query: str | None = None, limit: int = 50, sort_by: str = "last_active") -> list[dict[str, Any]]:
    """List WhatsApp chats. sort_by: 'last_active' or 'name'."""
    return wacli.list_chats(query=query, limit=limit, sort=sort_by)


@mcp.tool()
def get_chat(chat_jid: str) -> dict[str, Any]:
    """Get WhatsApp chat metadata by JID."""
    return wacli.get_chat(chat_jid)


@mcp.tool()
def get_direct_chat_by_contact(sender_phone_number: str) -> dict[str, Any]:
    """Get the 1:1 chat for a phone number."""
    digits = "".join(c for c in sender_phone_number if c.isdigit())
    return wacli.get_chat(f"{digits}@s.whatsapp.net")


@mcp.tool()
def get_contact_chats(jid: str, limit: int = 20) -> list[dict[str, Any]]:
    """Get all chats (direct + groups) involving a contact JID."""
    return wacli.get_contact_chats(jid, limit)


@mcp.tool()
def get_last_interaction(jid: str) -> dict[str, Any]:
    """Get the most recent message in a chat JID."""
    return wacli.get_last_interaction(jid)


@mcp.tool()
def get_message_context(message_id: str, before: int = 5, after: int = 5) -> dict[str, Any]:
    """Get the messages surrounding a specific message."""
    return wacli.get_message_context(message_id, before, after)


@mcp.tool()
def send_message(recipient: str, message: str) -> dict[str, Any]:
    """Send a WhatsApp text message. Recipient = phone number (no +/symbols) or JID."""
    if not recipient:
        return {"success": False, "message": "Recipient must be provided"}
    return wacli.send_text(recipient, message)


@mcp.tool()
def send_file(recipient: str, media_path: str, caption: str | None = None) -> dict[str, Any]:
    """Send a file (image/video/audio/document) to a recipient."""
    return wacli.send_file(recipient, media_path, caption=caption)


@mcp.tool()
def send_audio_message(recipient: str, media_path: str) -> dict[str, Any]:
    """Send an audio file as a WhatsApp voice note (OGG/Opus recommended)."""
    return wacli.send_file(recipient, media_path, ptt=True)


@mcp.tool()
def download_media(message_id: str, chat_jid: str) -> dict[str, Any]:
    """Download media from a message and return the local file path."""
    return wacli.download_media(chat_jid, message_id)


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
class _BearerAuthASGIMiddleware:
    """Static-token gate for HTTP mode when OAuth (AUTHKIT_DOMAIN) is not set."""

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if not hmac.compare_digest(headers.get(b"authorization", b""), self._expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b'Bearer realm="whatsapp-mcp"')]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def _run_http() -> None:
    import uvicorn

    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8081"))
    app = mcp.streamable_http_app()
    if _SDK_AUTH:
        print("Serving streamable-HTTP with OAuth resource-server auth (WorkOS AuthKit).", file=sys.stderr)
    else:
        token = os.getenv("MCP_AUTH_TOKEN", "").strip()
        if token:
            app = _BearerAuthASGIMiddleware(app, token)
        else:
            print("WARNING: no MCP_AUTH_TOKEN and no AUTHKIT_DOMAIN — serving without auth.", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("UVICORN_LOG_LEVEL", "info"))


def _shutdown(signum, frame):
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    if os.getenv("MCP_TRANSPORT", "stdio").strip().lower() in ("http", "streamable-http", "streamable_http"):
        _run_http()
    else:
        mcp.run(transport="stdio")
