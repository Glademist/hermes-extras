"""Live probe: run the full pipeline against the REAL session DB.

For the most recent sessions it resolves the anchored figure exactly as the
plugin would (``advisor.session_db.resolve``), builds the pressure reading, and
prints the new footer line beside the OLD footer's claim (``/1M``) so the
discrepancy — and the fix — are visible on real data.

Run inside the Hermes venv:
    ~/.hermes/hermes-agent/venv/bin/python tools/probe.py
"""

import os
import sys

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)

import hermes_state_registry  # noqa: E402  (Hermes venv)

from advisor import Budgets, Snapshot, build_pressure  # noqa: E402
from advisor.render import fmt_tokens, render_footer  # noqa: E402
from advisor import session_db  # noqa: E402

WINDOW = 1_048_576  # deepseek-v4.1-flash @ nous


def main() -> int:
    db = hermes_state_registry.acquire()
    try:
        rows = db.list_recent_sessions_bounded(limit=10)
    finally:
        hermes_state_registry.release(db)

    budgets = Budgets(window=WINDOW)
    print(f"window={fmt_tokens(WINDOW)}  compress_budget={fmt_tokens(budgets.compress_tokens)}"
          f"  hygiene_ceiling={fmt_tokens(budgets.hygiene_tokens)}  msg_valve={budgets.msg_limit}")
    print()
    for r in rows:
        sid = r.get("id")
        n = int(r.get("message_count") or 0)
        tokens, source = session_db.resolve(sid, turn_id="probe", provider_tokens=None)
        title = (r.get("title") or "")[:38]
        if not (isinstance(tokens, int) and tokens > 0):
            print(f"{title:38} n={n:4}  [no anchored figure — would fall back to provider]")
            continue
        p = build_pressure(Snapshot(tokens=tokens, source=source, message_count=n, budgets=budgets))
        print(f"{title:38} n={n:4}")
        print(f"    new: {render_footer(p)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
