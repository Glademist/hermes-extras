#!/usr/bin/env python3
"""Zdroj obsahu session pro Dreamer.

Hermes v0.21 zrušil automatické JSON snapshoty (`write_json_snapshots` je od
commitu 7a5fc1b2a9 "fix: remove automatic session JSON snapshots", 2026-09-07,
legacy no-op). Obsah session žije jen v SQLite `~/.hermes/state.db`, takže
Dreamer čte primárně odtud. Snapshot `session_{id}.json` zůstává jako fallback,
kdyby Hermes funkci někdy vrátil (a pro staré session, které ho ještě mají).

Klíčové: `last_updated` je odvozen z reálného času poslední zprávy
(MAX(messages.timestamp)), ne z indexového/channelového clocku — jinak se vrací
re-archive loop, kde session "roste" jen proto, že v kanálu psal někdo jiný.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from noemem_paths import HERMES_SESSIONS_DIR, HERMES_STATE_DB


def _snapshot_path(session_id: str, sessions_dir: Path) -> Path:
    return sessions_dir / f"session_{session_id}.json"


def _iso(ts) -> str:
    """Unix timestamp (real) → ISO string; prázdné/neplatné → ''."""
    try:
        return datetime.fromtimestamp(float(ts)).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _connect(db_path: Path):
    if not Path(db_path).exists():
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def content_timestamp(session_id: str, sessions_dir: Path | None = None) -> str | None:
    """Lehký content clock pro skip gate (bez načítání zpráv).

    Pořadí: snapshot `last_updated` (původní chování) → MAX(messages.timestamp)
    ve state.db → None (volající spadne na indexový updated_at).
    """
    sessions_dir = sessions_dir or HERMES_SESSIONS_DIR
    snap = _snapshot_path(session_id, sessions_dir)
    if snap.exists():
        try:
            value = json.loads(snap.read_text(encoding="utf-8")).get("last_updated")
            if value:
                return value
        except (OSError, json.JSONDecodeError):
            pass

    conn = _connect(HERMES_STATE_DB)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT MAX(timestamp) FROM messages WHERE session_id = ? AND active = 1",
            (session_id,),
        ).fetchone()
        return _iso(row[0]) if row and row[0] is not None else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _message_digest(session_id: str, sessions_dir: Path | None = None) -> str:
    """Fingerprint of the LAST USER message (state.db, no snapshot).

    content_timestamp() alone is not enough to stop the re-archive loop: a bot
    cron response appends assistant messages, so MAX(timestamp) advances while
    the conversation substance is frozen. The last user message only changes
    when a human actually wrote something new.
    """
    sessions_dir = sessions_dir or HERMES_SESSIONS_DIR
    snap = _snapshot_path(session_id, sessions_dir)
    if snap.exists():
        try:
            msgs = json.loads(snap.read_text(encoding="utf-8")).get("messages") or []
            for msg in reversed(msgs):
                if msg.get("role") == "user":
                    body = json.dumps(
                        {"content": msg.get("content", ""), "tool_calls": msg.get("tool_calls", "")},
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:64]
            return ""
        except (OSError, json.JSONDecodeError):
            pass

    conn = _connect(HERMES_STATE_DB)
    if conn is None:
        return ""
    try:
        row = conn.execute(
            "SELECT content FROM messages WHERE session_id = ? AND active = 1"
            " AND role = 'user' ORDER BY timestamp DESC, id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row and row[0]:
            return hashlib.sha256(str(row[0]).encode("utf-8")).hexdigest()[:64]
        return ""
    except sqlite3.Error:
        return ""
    finally:
        conn.close()


def load_session_content(session_id: str, sessions_dir: Path | None = None) -> dict | None:
    """Vrátí dict ve tvaru starého snapshotu (pro `session_clean.clean_session`)."""
    sessions_dir = sessions_dir or HERMES_SESSIONS_DIR
    snap = _snapshot_path(session_id, sessions_dir)
    if snap.exists():
        try:
            return json.loads(snap.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    conn = _connect(HERMES_STATE_DB)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT id, source, model, system_prompt, started_at, display_name, title"
            " FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None

        messages = []
        last_ts = None
        for role, content, tool_call_id, tool_calls, tool_name, ts in conn.execute(
            "SELECT role, content, tool_call_id, tool_calls, tool_name, timestamp"
            " FROM messages WHERE session_id = ? AND active = 1 ORDER BY id",
            (session_id,),
        ):
            msg = {"role": role}
            if content:
                msg["content"] = content
            if tool_call_id:
                msg["tool_call_id"] = tool_call_id
            if tool_calls:
                msg["tool_calls"] = tool_calls
            if tool_name:
                msg["tool_name"] = tool_name
            messages.append(msg)
            if ts is not None:
                last_ts = ts if last_ts is None else max(last_ts, ts)

        if not messages:
            return None

        return {
            "session_id": row[0],
            "model": row[2] or "",
            "base_url": "",
            "platform": row[1] or "",
            "session_start": _iso(row[4]),
            "last_updated": _iso(last_ts),
            "display_name": row[5] or row[6] or "",
            "system_prompt": row[3] or "",
            "tools": [],
            "message_count": len(messages),
            "messages": messages,
        }
    except sqlite3.Error:
        return None
    finally:
        conn.close()
