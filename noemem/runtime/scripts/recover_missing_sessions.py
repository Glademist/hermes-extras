#!/usr/bin/env python3
"""Recover 3 missing session content files from state.db (2026-08-19).

Triage 'content file chybí' je oddělený problém od path fixu. Tyto sessiony
jsou v state.db ale chybí jim ~/.hermes/sessions/session_{id}.json snapshot.
"""
import sqlite3
import json
import os
from datetime import datetime

DB_PATH = "~/.hermes/state.db"
SESSIONS_DIR = "~/.hermes/sessions"

MISSING_IDS = [
    "20260528_080540_26fb78d6",  # teacher kanban blocked karta
    "20260528_074844_7ee2ba8e",  # xnotes oof a zase znovu
    "20260813_105834_1a553722",  # entropy zkoušel update hermes
]

conn = sqlite3.connect(DB_PATH)

for sid in MISSING_IDS:
    outpath = os.path.join(SESSIONS_DIR, f"session_{sid}.json")
    if os.path.exists(outpath):
        print(f"⏭️  {sid}: už existuje, přeskočeno")
        continue

    cur = conn.execute(
        "SELECT id, source, message_count, started_at, ended_at, model, system_prompt FROM sessions WHERE id = ?",
        (sid,),
    )
    session_row = cur.fetchone()
    if not session_row:
        print(f"❌ {sid}: not found in state.db")
        continue

    cur = conn.execute(
        "SELECT id, role, content, tool_call_id, tool_calls, tool_name, timestamp FROM messages WHERE session_id = ? ORDER BY id",
        (sid,),
    )
    messages = []
    for mrow in cur:
        msg = {"role": mrow[1]}
        if mrow[2]:
            msg["content"] = mrow[2]
        if mrow[3]:
            msg["tool_call_id"] = mrow[3]
        if mrow[4]:
            msg["tool_calls"] = mrow[4]
        if mrow[5]:
            msg["tool_name"] = mrow[5]
        messages.append(msg)

    entry = {
        "session_id": sid,
        "model": session_row[5] or "",
        "base_url": "",
        "platform": "discord",
        "session_start": datetime.fromtimestamp(session_row[3]).isoformat() if session_row[3] else "",
        "last_updated": datetime.fromtimestamp(session_row[4]).isoformat() if session_row[4] else datetime.now().isoformat(),
        "system_prompt": session_row[6] or "",
        "tools": [],
        "message_count": len(messages),
        "messages": messages,
    }

    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2, ensure_ascii=False)
    print(f"✅ {sid}: {len(messages)} messages → {outpath}")

conn.close()
print("\n🏁 Done.")
