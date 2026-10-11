"""Diagnose WHY the usage anchor fails to match (the (provider) fallback rate).

For each recent session it shows whether the persisted anchor still matches the
DB-loaded transcript, and when it does not, the exact mismatch: the anchor's
``base_count-1`` slot reads ``role=tool`` from capture while the reloaded
transcript shows another role there — a positional shift, not corruption.

Finding (2026-10-07): idle/closed sessions match; sessions with an in-flight turn
shift. See design.md §5.1.

Run: ~/.hermes/hermes-agent/venv/bin/python tools/anchor_probe.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))

import hermes_state_registry  # noqa: E402
from agent.usage_anchor import _serialize, _anchor_matches, message_fingerprint  # noqa: E402


def main() -> int:
    db = hermes_state_registry.acquire()
    try:
        rows = db.list_recent_sessions_bounded(limit=8)
        for r in rows:
            sid = r.get("id")
            msgs = db.get_messages_as_conversation(sid)
            raw = db.get_session_model_config_value(sid, "_usage_anchor", None)
            anchor = _serialize(raw) if raw else None
            title = (r.get("title") or "")[:34]
            if not anchor:
                print(f"{title:34} n={len(msgs):4}  anchor=None")
                continue
            bc = anchor["base_count"]
            match = _anchor_matches(msgs, anchor)
            if 0 < bc <= len(msgs):
                bm = msgs[bc - 1]
                detail = (f"base_count={bc} n={len(msgs)} "
                          f"role_ok={bm.get('role') == anchor.get('base_last_role')} "
                          f"fp_ok={message_fingerprint(bm) == anchor.get('base_last_fp')} "
                          f"stored={anchor.get('base_last_role')} db={bm.get('role')}")
            else:
                detail = f"base_count={bc} n={len(msgs)} OUT_OF_RANGE"
            print(f"{title:34} match={match!s:5}  {detail}")
    finally:
        hermes_state_registry.release(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())