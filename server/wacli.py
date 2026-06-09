"""Backend for the WhatsApp MCP server, built on the `wacli` engine.

Reads go through wacli's own read commands (`messages list`, `chats list`,
`contacts search`, `calls list`, ...) — `--json`, lock-free (`needLock=false`),
so they run fine alongside the always-on `sync --follow` daemon. This keeps us on
wacli's stable CLI contract instead of coupling to its internal DB schema.

Three reads have no clean wacli read command, so they query the read-only SQLite
store directly: `get_contact_chats` (cross-table join), `get_group_info`
(participants have no read subcommand; `groups info` is a live, lock-needing
command), and `get_message_context` (`messages context` requires `--chat`, which
that tool doesn't receive).

Writes (send / media download) shell out to `wacli`, which owns the live
connection (delegating sends to the daemon over its socket). We never write the DB.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WACLI_BIN = os.getenv("WACLI_BIN", "wacli")
STORE_DIR = os.getenv("WACLI_STORE_DIR", os.path.expanduser("~/.wacli"))
CLI_TIMEOUT = int(os.getenv("WACLI_CLI_TIMEOUT", "60"))


# --------------------------------------------------------------------------- #
# wacli command runner (shared by reads and writes)
# --------------------------------------------------------------------------- #
def _run(args: list[str]) -> dict[str, Any]:
    """Run `wacli <args> --json` and return the parsed envelope.

    wacli wraps all `--json` output as {"success", "data", "error"}; we surface
    that as-is (plus a top-level "success"). On process error returns
    {"success": False, "message": ...}.
    """
    cmd = [WACLI_BIN, *args, "--json"]
    env = {**os.environ, "WACLI_STORE_DIR": STORE_DIR}
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT, env=env
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "wacli timed out"}
    if p.returncode != 0:
        return {"success": False, "message": (p.stderr or p.stdout or "wacli error").strip()}
    out = (p.stdout or "").strip()
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {"raw": out}
    return {"success": True, **(data if isinstance(data, dict) else {"result": data})}


def _read(args: list[str]) -> Any:
    """Run a wacli read command and return its inner `data` payload (or None)."""
    res = _run(args)
    if not res.get("success"):
        return None
    return res.get("data")


# --------------------------------------------------------------------------- #
# JSON → tool-shape mappers (wacli read output)
# --------------------------------------------------------------------------- #
def _ts_str(s: Any) -> str | None:
    """Pass through wacli RFC3339 timestamps; map Go zero-time to None."""
    if not s or (isinstance(s, str) and s.startswith("0001-01-01")):
        return None
    return s


def _msg_from_json(m: dict[str, Any]) -> dict[str, Any]:
    """Map a wacli `store.Message` JSON object (mostly PascalCase fields)."""
    return {
        "msg_id": m.get("MsgID"),
        "chat_jid": m.get("ChatJID"),
        "chat_name": m.get("ChatName"),
        "sender_jid": m.get("SenderJID"),
        "sender_name": m.get("SenderName"),
        "from_me": bool(m.get("FromMe")),
        "timestamp": _ts_str(m.get("Timestamp")),
        "text": m.get("DisplayText") or m.get("Text"),
        "media_type": m.get("MediaType") or None,
        "media_caption": m.get("MediaCaption") or None,
        "filename": m.get("Filename") or None,
        "quoted_msg_id": m.get("quoted_msg_id") or None,  # tagged json field
        "is_forwarded": bool(m.get("IsForwarded")),
        "reaction_emoji": m.get("ReactionEmoji") or None,
        "snippet": m.get("Snippet") or None,
    }


def _chat_from_json(c: dict[str, Any]) -> dict[str, Any]:
    """Map a wacli chat JSON object (snake_case fields)."""
    return {
        "jid": c.get("jid"),
        "name": c.get("name") or c.get("jid"),
        "kind": c.get("kind"),
        "last_message_time": _ts_str(c.get("last_message_ts")),
        "archived": bool(c.get("archived")),
        "pinned": bool(c.get("pinned")),
        "unread_count": c.get("unread_count") or 0,
    }


def _contact_from_json(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "jid": c.get("jid"),
        "phone": c.get("phone"),
        "name": c.get("name") or c.get("alias") or c.get("system_name") or None,
    }


def _call_from_json(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "chat_jid": c.get("chat_jid"),
        "chat_name": c.get("chat_name"),
        "from": c.get("sender_name") or c.get("sender_jid"),
        "event": c.get("event_type"),
        "direction": c.get("direction"),
        "media": c.get("media"),
        "outcome": c.get("outcome"),
        "call_type": c.get("call_type"),
        "duration_secs": c.get("duration_secs") or 0,
        "timestamp": _ts_str(c.get("timestamp")),
    }


# --------------------------------------------------------------------------- #
# Reads via wacli commands (lock-free, on wacli's stable CLI contract)
# --------------------------------------------------------------------------- #
def list_messages(
    chat_jid: str | None = None,
    sender_jid: str | None = None,
    query: str | None = None,
    limit: int = 50,
    after: str | None = None,
    before: str | None = None,
    from_me: bool | None = None,
    sort: str = "newest",
) -> list[dict[str, Any]]:
    lim = str(min(int(limit), 500))
    if query:
        # `messages list` has no text filter; route text queries to FTS search.
        args = ["messages", "search", query, "--limit", lim]
        if chat_jid:
            args += ["--chat", chat_jid]
        if sender_jid:
            args += ["--from", sender_jid]
        if after:
            args += ["--after", after]
        if before:
            args += ["--before", before]
    else:
        args = ["messages", "list", "--limit", lim]
        if chat_jid:
            args += ["--chat", chat_jid]
        if sender_jid:
            args += ["--sender", sender_jid]
        if after:
            args += ["--after", after]
        if before:
            args += ["--before", before]
        if from_me is True:
            args += ["--from-me"]
        elif from_me is False:
            args += ["--from-them"]
        if sort == "oldest":
            args += ["--asc"]
    data = _read(args) or {}
    return [_msg_from_json(m) for m in (data.get("messages") or [])]


def list_chats(
    query: str | None = None, limit: int = 50, sort: str = "last_active"
) -> list[dict[str, Any]]:
    args = ["chats", "list", "--limit", str(min(int(limit), 200))]
    if query:
        args += ["--query", query]
    data = _read(args) or []
    out = [_chat_from_json(c) for c in data]
    if sort == "name":
        out.sort(key=lambda x: (x["name"] or "").lower())
    return out


def get_chat(chat_jid: str) -> dict[str, Any]:
    data = _read(["chats", "show", "--jid", chat_jid])
    return _chat_from_json(data) if isinstance(data, dict) else {}


def search_contacts(query: str) -> list[dict[str, Any]]:
    data = _read(["contacts", "search", query, "--limit", "50"]) or []
    return [_contact_from_json(c) for c in data]


def get_contact(identifier: str) -> dict[str, Any]:
    ident = identifier.strip()
    jid = ident if "@" in ident else f"{''.join(c for c in ident if c.isdigit())}@s.whatsapp.net"
    data = _read(["contacts", "show", "--jid", jid])
    if not isinstance(data, dict):
        return {"jid": jid, "resolved": False}
    return {**_contact_from_json(data), "resolved": True}


def get_last_interaction(jid: str) -> dict[str, Any]:
    data = _read(["messages", "list", "--chat", jid, "--limit", "1"]) or {}
    msgs = data.get("messages") or []
    return _msg_from_json(msgs[0]) if msgs else {}


def list_recent_calls(chat_jid: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    args = ["calls", "list", "--limit", str(min(int(limit), 200))]
    if chat_jid:
        args += ["--chat", chat_jid]
    data = _read(args) or {}
    return [_call_from_json(c) for c in (data.get("calls") or [])]


def list_starred_messages(chat_jid: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    args = ["messages", "list", "--starred", "--limit", str(min(int(limit), 200))]
    if chat_jid:
        args += ["--chat", chat_jid]
    data = _read(args) or {}
    return [_msg_from_json(m) for m in (data.get("messages") or [])]


def search_messages(
    query: str,
    chat_jid: str | None = None,
    sender_jid: str | None = None,
    limit: int = 50,
    after: str | None = None,
    before: str | None = None,
    has_media: bool = False,
    msg_type: str | None = None,
) -> list[dict[str, Any]]:
    """Full-text (FTS5) message search via the wacli engine."""
    args = ["messages", "search", query, "--limit", str(min(int(limit), 200))]
    if chat_jid:
        args += ["--chat", chat_jid]
    if sender_jid:
        args += ["--from", sender_jid]
    if after:
        args += ["--after", after]
    if before:
        args += ["--before", before]
    if has_media:
        args += ["--has-media"]
    if msg_type:
        args += ["--type", msg_type]
    data = _read(args) or {}
    return [_msg_from_json(m) for m in (data.get("messages") or [])]


# --------------------------------------------------------------------------- #
# Reads with no clean wacli command → read-only SQLite over wacli.db
# --------------------------------------------------------------------------- #
def _db_path() -> str:
    return os.getenv("WACLI_DB_PATH", str(Path(STORE_DIR) / "wacli.db"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _iso(ts: Any) -> str | None:
    if ts in (None, 0):
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, OSError, TypeError):
        return None


_MSG_COLS = (
    "msg_id, chat_jid, chat_name, sender_jid, sender_name, from_me, ts, text, "
    "display_text, media_type, media_caption, filename, quoted_msg_id, "
    "is_forwarded, reaction_emoji, edited"
)


def _msg_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    return {
        "msg_id": d.get("msg_id"),
        "chat_jid": d.get("chat_jid"),
        "chat_name": d.get("chat_name"),
        "sender_jid": d.get("sender_jid"),
        "sender_name": d.get("sender_name"),
        "from_me": bool(d.get("from_me")),
        "timestamp": _iso(d.get("ts")),
        "text": d.get("display_text") or d.get("text"),
        "media_type": d.get("media_type"),
        "media_caption": d.get("media_caption"),
        "filename": d.get("filename"),
        "quoted_msg_id": d.get("quoted_msg_id"),
        "is_forwarded": bool(d.get("is_forwarded")),
        "reaction_emoji": d.get("reaction_emoji"),
        "edited": bool(d.get("edited")),
    }


def get_contact_chats(jid: str, limit: int = 20) -> list[dict[str, Any]]:
    # Direct chat + any group where this user participates. No wacli command
    # covers this join, so we read the store directly.
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT c.jid, c.kind, c.name, c.last_message_ts FROM chats c "
            "LEFT JOIN group_participants g ON g.group_jid = c.jid "
            "WHERE c.jid = ? OR g.user_jid = ? ORDER BY c.last_message_ts DESC LIMIT ?",
            (jid, jid, min(int(limit), 200)),
        )
        return [
            {"jid": d["jid"], "name": d.get("name") or d["jid"], "kind": d.get("kind"),
             "last_message_time": _iso(d.get("last_message_ts"))}
            for d in (dict(r) for r in rows)
        ]


def get_message_context(
    msg_id: str, before: int = 5, after: int = 5
) -> dict[str, Any]:
    # `wacli messages context` requires --chat, which this tool isn't given, so
    # we read the store directly and split around the target message.
    with _connect() as conn:
        target = conn.execute(
            f"SELECT {_MSG_COLS}, ts AS _ts FROM messages WHERE msg_id = ? LIMIT 1",
            (msg_id,),
        ).fetchone()
        if not target:
            return {}
        chat = target["chat_jid"]
        ts = target["_ts"]
        bef = conn.execute(
            f"SELECT {_MSG_COLS} FROM messages WHERE chat_jid = ? AND ts < ? "
            "AND revoked = 0 ORDER BY ts DESC LIMIT ?",
            (chat, ts, int(before)),
        )
        aft = conn.execute(
            f"SELECT {_MSG_COLS} FROM messages WHERE chat_jid = ? AND ts > ? "
            "AND revoked = 0 ORDER BY ts ASC LIMIT ?",
            (chat, ts, int(after)),
        )
        return {
            "message": _msg_row(target),
            "before": [_msg_row(r) for r in reversed(bef.fetchall())],
            "after": [_msg_row(r) for r in aft.fetchall()],
        }


def get_group_info(group_jid: str) -> dict[str, Any]:
    # `groups info` is a live, lock-needing command and `groups participants` has
    # no read subcommand, so we read group metadata + participants from the store.
    with _connect() as conn:
        g = conn.execute(
            "SELECT jid, name, owner_jid, created_ts, is_parent, left_at "
            "FROM groups WHERE jid = ?",
            (group_jid,),
        ).fetchone()
        if not g:
            return {}
        parts = conn.execute(
            "SELECT user_jid, role FROM group_participants WHERE group_jid = ? "
            "ORDER BY role DESC, user_jid",
            (group_jid,),
        ).fetchall()
        d = dict(g)
        return {
            "jid": d["jid"],
            "name": d.get("name"),
            "owner_jid": d.get("owner_jid"),
            "created": _iso(d.get("created_ts")),
            "is_community": bool(d.get("is_parent")),
            "left": bool(d.get("left_at")),
            "participant_count": len(parts),
            "participants": [{"jid": p["user_jid"], "role": p["role"] or "member"} for p in parts],
        }


# --------------------------------------------------------------------------- #
# Write path: shell out to the wacli binary
# --------------------------------------------------------------------------- #
def send_text(
    recipient: str,
    message: str,
    reply_to: str | None = None,
    mentions: list[str] | None = None,
) -> dict[str, Any]:
    args = ["send", "text", "--to", recipient, "--message", message]
    if reply_to:
        args += ["--reply-to", reply_to]
    for m in mentions or []:
        args += ["--mention", m]
    return _run(args)


def mark_chat_read(chat_jid: str) -> dict[str, Any]:
    return _run(["chats", "mark-read", "--chat", chat_jid])


def request_history(chat_jid: str, count: int = 100) -> dict[str, Any]:
    return _run(["history", "backfill", "--chat", chat_jid, "--count", str(int(count))])


def send_file(recipient: str, media_path: str, caption: str | None = None, ptt: bool = False) -> dict[str, Any]:
    args = ["send", "file", "--to", recipient, "--file", media_path]
    if caption:
        args += ["--caption", caption]
    if ptt:
        args += ["--ptt"]
    return _run(args)


def download_media(chat_jid: str, msg_id: str, output: str | None = None) -> dict[str, Any]:
    # Read-only download: wacli fetches the encrypted blob straight from WhatsApp's
    # CDN using the DirectPath/MediaKey already in the synced DB, via
    # newApp(..., needLock=False, ...). It never contends for the store lock held by
    # the always-on `sync --follow` daemon. Read-only mode requires an --output dir.
    out = output or os.path.join(STORE_DIR, "downloads")
    os.makedirs(out, exist_ok=True)
    return _run(["media", "download", "--chat", chat_jid, "--id", msg_id, "--output", out, "--read-only"])


def transcribe_audio(chat_jid: str, msg_id: str) -> dict[str, Any]:
    """Download a voice note / audio message via wacli and transcribe it with the
    OpenAI Whisper API. Requires OPENAI_API_KEY. Returns {success, text}."""
    import glob
    import tempfile

    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return {"success": False, "message": "Transcription not configured (set OPENAI_API_KEY)."}

    out_dir = tempfile.mkdtemp(prefix="wa_tx_")
    # --read-only downloads straight from WhatsApp's CDN (no store lock), so this
    # works while the sync daemon holds the session. See download_media() above.
    dl = _run(["media", "download", "--chat", chat_jid, "--id", msg_id, "--output", out_dir, "--read-only"])
    if not dl.get("success"):
        return {"success": False, "message": f"download failed: {dl.get('message', 'unknown error')}"}
    files = [f for f in glob.glob(os.path.join(out_dir, "*")) if os.path.isfile(f)]
    if not files:
        return {"success": False, "message": "no media file found (message may have no downloadable audio)"}
    path = files[0]

    import httpx

    model = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
    try:
        with open(path, "rb") as f:
            resp = httpx.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                data={"model": model},
                files={"file": (os.path.basename(path), f, "application/octet-stream")},
                timeout=180,
            )
    except Exception as e:  # noqa: BLE001
        return {"success": False, "message": f"transcription request failed: {e}"}
    finally:
        for f in files:
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            os.rmdir(out_dir)
        except OSError:
            pass

    if resp.status_code != 200:
        return {"success": False, "message": f"OpenAI error {resp.status_code}: {resp.text[:200]}"}
    return {"success": True, "text": resp.json().get("text", "")}


def react(recipient: str, msg_id: str, emoji: str = "👍", sender: str | None = None) -> dict[str, Any]:
    args = ["send", "react", "--to", recipient, "--id", msg_id, "--reaction", emoji]
    if sender:
        args += ["--sender", sender]
    return _run(args)
