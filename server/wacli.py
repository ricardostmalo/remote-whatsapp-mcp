"""Backend for the WhatsApp MCP server, built on the `wacli` engine.

Reads come from a read-only SQLite connection to wacli's `wacli.db` (schema:
chats, contacts, messages, ...). Writes (send / media download) shell out to the
`wacli` binary, which owns the live WhatsApp connection. We never write to the DB.
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
# Read path: read-only SQLite over wacli.db
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


def _epoch(s: str | None) -> int | None:
    """Parse YYYY-MM-DD or RFC3339 into a unix timestamp (seconds)."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


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


_MSG_COLS = (
    "msg_id, chat_jid, chat_name, sender_jid, sender_name, from_me, ts, text, "
    "display_text, media_type, media_caption, filename, quoted_msg_id, "
    "is_forwarded, reaction_emoji, edited"
)


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
    where = ["revoked = 0", "deleted_for_me = 0"]
    params: list[Any] = []
    if chat_jid:
        where.append("chat_jid = ?")
        params.append(chat_jid)
    if sender_jid:
        where.append("sender_jid = ?")
        params.append(sender_jid)
    if query:
        where.append("(display_text LIKE ? OR text LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    if from_me is not None:
        where.append("from_me = ?")
        params.append(1 if from_me else 0)
    a, b = _epoch(after), _epoch(before)
    if a:
        where.append("ts >= ?")
        params.append(a)
    if b:
        where.append("ts <= ?")
        params.append(b)
    order = "ASC" if sort == "oldest" else "DESC"
    sql = (
        f"SELECT {_MSG_COLS} FROM messages WHERE {' AND '.join(where)} "
        f"ORDER BY ts {order} LIMIT ?"
    )
    params.append(min(int(limit), 500))
    with _connect() as conn:
        return [_msg_row(r) for r in conn.execute(sql, params)]


def list_chats(
    query: str | None = None, limit: int = 50, sort: str = "last_active"
) -> list[dict[str, Any]]:
    where, params = [], []
    if query:
        where.append("(name LIKE ? OR jid LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    order = "name COLLATE NOCASE ASC" if sort == "name" else "last_message_ts DESC"
    sql = (
        "SELECT jid, kind, name, last_message_ts, archived, pinned, unread_count "
        f"FROM chats {clause} ORDER BY {order} LIMIT ?"
    )
    params.append(min(int(limit), 200))
    with _connect() as conn:
        out = []
        for r in conn.execute(sql, params):
            d = dict(r)
            out.append(
                {
                    "jid": d["jid"],
                    "name": d.get("name") or d["jid"],
                    "kind": d.get("kind"),
                    "last_message_time": _iso(d.get("last_message_ts")),
                    "archived": bool(d.get("archived")),
                    "pinned": bool(d.get("pinned")),
                    "unread_count": d.get("unread_count") or 0,
                }
            )
        return out


def get_chat(chat_jid: str) -> dict[str, Any]:
    with _connect() as conn:
        r = conn.execute(
            "SELECT jid, kind, name, last_message_ts, archived, pinned, unread_count "
            "FROM chats WHERE jid = ?",
            (chat_jid,),
        ).fetchone()
        if not r:
            return {}
        d = dict(r)
        return {
            "jid": d["jid"],
            "name": d.get("name") or d["jid"],
            "kind": d.get("kind"),
            "last_message_time": _iso(d.get("last_message_ts")),
            "archived": bool(d.get("archived")),
            "pinned": bool(d.get("pinned")),
            "unread_count": d.get("unread_count") or 0,
        }


def search_contacts(query: str) -> list[dict[str, Any]]:
    like = f"%{query}%"
    sql = (
        "SELECT jid, phone, push_name, full_name, business_name, system_name "
        "FROM contacts WHERE jid LIKE ? OR phone LIKE ? OR push_name LIKE ? "
        "OR full_name LIKE ? OR business_name LIKE ? OR system_name LIKE ? LIMIT 50"
    )
    with _connect() as conn:
        out = []
        for r in conn.execute(sql, [like] * 6):
            d = dict(r)
            out.append(
                {
                    "jid": d["jid"],
                    "phone": d.get("phone"),
                    "name": d.get("full_name")
                    or d.get("push_name")
                    or d.get("business_name")
                    or d.get("system_name"),
                }
            )
        return out


def get_contact(identifier: str) -> dict[str, Any]:
    ident = identifier.strip()
    jid = ident if "@" in ident else f"{''.join(c for c in ident if c.isdigit())}@s.whatsapp.net"
    with _connect() as conn:
        r = conn.execute(
            "SELECT jid, phone, push_name, full_name, business_name, system_name "
            "FROM contacts WHERE jid = ? OR phone = ?",
            (jid, ident),
        ).fetchone()
        if not r:
            return {"jid": jid, "resolved": False}
        d = dict(r)
        return {
            "jid": d["jid"],
            "phone": d.get("phone"),
            "name": d.get("full_name") or d.get("push_name") or d.get("business_name"),
            "resolved": True,
        }


def get_contact_chats(jid: str, limit: int = 20) -> list[dict[str, Any]]:
    # Direct chat + any group where this user participates.
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


def get_last_interaction(jid: str) -> dict[str, Any]:
    with _connect() as conn:
        r = conn.execute(
            f"SELECT {_MSG_COLS} FROM messages WHERE chat_jid = ? AND revoked = 0 "
            "ORDER BY ts DESC LIMIT 1",
            (jid,),
        ).fetchone()
        return _msg_row(r) if r else {}


def get_message_context(
    msg_id: str, before: int = 5, after: int = 5
) -> dict[str, Any]:
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


# --------------------------------------------------------------------------- #
# Write path: shell out to the wacli binary
# --------------------------------------------------------------------------- #
def _run(args: list[str]) -> dict[str, Any]:
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


def list_recent_calls(chat_jid: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    where, params = [], []
    if chat_jid:
        where.append("chat_jid = ?")
        params.append(chat_jid)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (
        "SELECT chat_jid, chat_name, sender_jid, sender_name, event_type, direction, "
        "media, outcome, call_type, duration_secs, ts "
        f"FROM call_events {clause} ORDER BY ts DESC LIMIT ?"
    )
    params.append(min(int(limit), 200))
    with _connect() as conn:
        out = []
        for r in conn.execute(sql, params):
            d = dict(r)
            out.append({
                "chat_jid": d["chat_jid"],
                "chat_name": d.get("chat_name"),
                "from": d.get("sender_name") or d.get("sender_jid"),
                "event": d.get("event_type"),
                "direction": d.get("direction"),
                "media": d.get("media"),
                "outcome": d.get("outcome"),
                "call_type": d.get("call_type"),
                "duration_secs": d.get("duration_secs") or 0,
                "timestamp": _iso(d.get("ts")),
            })
        return out


def list_starred_messages(chat_jid: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    where, params = [], []
    if chat_jid:
        where.append("s.chat_jid = ?")
        params.append(chat_jid)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (
        "SELECT m.msg_id, m.chat_jid, m.chat_name, m.sender_jid, m.sender_name, m.from_me, "
        "m.ts, m.text, m.display_text, m.media_type, m.media_caption, m.filename, "
        "m.quoted_msg_id, m.is_forwarded, m.reaction_emoji, m.edited "
        "FROM starred s JOIN messages m ON m.chat_jid = s.chat_jid AND m.msg_id = s.msg_id "
        f"{clause} ORDER BY s.starred_at DESC LIMIT ?"
    )
    params.append(min(int(limit), 200))
    with _connect() as conn:
        return [_msg_row(r) for r in conn.execute(sql, params)]


def get_group_info(group_jid: str) -> dict[str, Any]:
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


def send_file(recipient: str, media_path: str, caption: str | None = None, ptt: bool = False) -> dict[str, Any]:
    args = ["send", "file", "--to", recipient, "--file", media_path]
    if caption:
        args += ["--caption", caption]
    if ptt:
        args += ["--ptt"]
    return _run(args)


def download_media(chat_jid: str, msg_id: str, output: str | None = None) -> dict[str, Any]:
    args = ["media", "download", "--chat", chat_jid, "--id", msg_id]
    if output:
        args += ["--output", output]
    return _run(args)


def react(recipient: str, msg_id: str, emoji: str = "👍", sender: str | None = None) -> dict[str, Any]:
    args = ["send", "react", "--to", recipient, "--id", msg_id, "--reaction", emoji]
    if sender:
        args += ["--sender", sender]
    return _run(args)


def _search_msg(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "msg_id": m.get("MsgID"),
        "chat_jid": m.get("ChatJID"),
        "chat_name": m.get("ChatName"),
        "sender_jid": m.get("SenderJID"),
        "sender_name": m.get("SenderName"),
        "from_me": bool(m.get("FromMe")),
        "timestamp": m.get("Timestamp"),
        "text": m.get("DisplayText") or m.get("Text"),
        "media_type": m.get("MediaType") or None,
        "snippet": m.get("Snippet"),
    }


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
    res = _run(args)
    msgs = ((res.get("data") or {}).get("messages")) or []
    return [_search_msg(m) for m in msgs]
