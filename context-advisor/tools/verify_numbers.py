import os, sys, json
sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))
import hermes_state_registry
from agent.usage_anchor import persisted_anchor_tokens

WINDOW = 1_048_576
BUDGET = int(WINDOW * 0.50)
MSG_LIMIT = 400

def r5(pct):
    return int(pct / 5 + 0.5) * 5

db = hermes_state_registry.acquire()
try:
    rows = db.list_recent_sessions_bounded(limit=10)
    print(f"BUDGET={BUDGET}  MSG_LIMIT={MSG_LIMIT}")
    print(f"{'title':36} {'msgs':>5} {'row_cols'}")
    # dump columns of one session row
    full = db.get_session(rows[0]["id"]) or {}
    print("SESSION ROW COLUMNS:", sorted(full.keys()))
    print("TOKEN-ISH:", {k: v for k, v in full.items() if "token" in k.lower() or "usage" in k.lower()})
    print()
    for r in rows:
        sid = r["id"]
        n = int(r.get("message_count") or 0)
        msgs = db.get_messages_as_conversation(sid)
        anchored = persisted_anchor_tokens(db, sid, msgs)
        ctxpct = f"{r5(anchored/BUDGET*100)}%" if anchored else "n/a"
        msgpct = f"{r5(n/MSG_LIMIT*100)}%"
        print(f"{(r.get('title') or '')[:36]:36} n={n:4} anchored={anchored} -> Ctx {ctxpct} · Msg {msgpct}")
finally:
    hermes_state_registry.release(db)