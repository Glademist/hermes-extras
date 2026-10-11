"""Find the transcript loader whose message shape matches the persisted anchor.

The active-session mismatch (design.md §5.1) is a loader-shape question: the
anchor's fingerprint was captured on the LIVE messages list. If a different
SessionDB loader reconstructs the same shape, `persisted_anchor_tokens` will match
where `get_messages_as_conversation` does not.

Run: ~/.hermes/hermes-agent/venv/bin/python tools/loader_probe.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))

import hermes_state_registry  # noqa: E402
from agent.usage_anchor import _serialize, _anchor_matches  # noqa: E402


def _loaders():
    yield "get_messages_as_conversation", lambda db, sid: db.get_messages_as_conversation(sid)
    yield ("get_messages_as_conversation(repair)", lambda db, sid: db.get_messages_as_conversation(sid, repair_alternation=True))
    yield ("get_messages_as_conversation(compact)", lambda db, sid: db.get_messages_as_conversation(sid, include_compacted=True))
    if hasattr(hermes_state_registry, "acquire"):
        yield "get_messages", lambda db, sid: db.get_messages(sid)
        yield ("get_messages(compact)", lambda db, sid: db.get_messages(sid, include_compacted=True))


def main() -> int:
    db = hermes_state_registry.acquire()
    try:
        rows = db.list_recent_sessions_bounded(limit=8)
        for r in rows:
            sid = r.get("id")
            raw = db.get_session_model_config_value(sid, "_usage_anchor", None)
            anchor = _serialize(raw) if raw else None
            title = (r.get("title") or "")[:30]
            if not anchor:
                print(f"{title:30} anchor=None")
                continue
            results = []
            for name, fn in _loaders():
                try:
                    msgs = fn(db, sid)
                    results.append(f"{name}={'Y' if _anchor_matches(msgs, anchor) else 'n'}(n={len(msgs)})")
                except Exception as e:
                    results.append(f"{name}=ERR({type(e).__name__})")
            print(f"{title:30} {anchor.get('base_last_role'):9} " + "  ".join(results))
    finally:
        hermes_state_registry.release(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())