#!/usr/bin/env python3
"""quarantine_audit.py — daily watchdog for the archive quarantine gate.

Answers one question: is the lint gate hiding true facts RIGHT NOW?

Outputs a line ONLY when there is something to act on (watchdog pattern —
empty stdout = healthy = cron sends nothing):

  1. FALSE POSITIVES — quarantined records that pass the CURRENT lint rules.
     These were locked out by yesterday's rules and should be released.
  2. NEW QUARANTINES — records quarantined since the last audit run
     (state file remembers the seen set). Every new quarantine must be
     seen by a human, never silently excluded.

Exit 0 always (fail-open: a broken audit must never crash the cron).
Run: python3 quarantine_audit.py        (used by cron, no_agent=true)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ARCHIVE = Path.home() / "hermes-noemem" / "archive"
LINT = Path.home() / "hermes-noemem" / "runtime" / "scripts" / "archive_lint.py"
# state: set of archive files already reported as newly-quarantined
STATE = Path.home() / "hermes-noemem" / "runtime" / "state" / "quarantine_audit_state.json"


def quarantined_files() -> list[Path]:
    out = []
    for f in sorted(ARCHIVE.glob("*.md")):
        # read only the frontmatter head — cheap, hundreds of files stay fast
        head = f.read_text(encoding="utf-8", errors="replace")[:800]
        if "retrieval_tier: quarantine" in head:
            out.append(f)
    return out


def lint_clean(path: Path) -> bool:
    """Re-lint with the CURRENT rules; True when no errors remain."""
    try:
        r = subprocess.run(
            [sys.executable, str(LINT), "--file", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False  # cannot verify → do not call it a false positive


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    # load state (seen quarantine names), tolerate a missing/corrupt file
    seen: dict = {}
    if STATE.exists():
        try:
            seen = json.loads(STATE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            seen = {}

    files = quarantined_files()
    names = {f.name for f in files}
    lines = []

    # 1) false positives: still quarantined but CLEAN under today's rules
    for f in files:
        if lint_clean(f):
            lines.append(f"FALSE POSITIVE (passes current lint, still quarantined): {f.name}")

    # 2) new quarantines the user has not yet been shown
    new = sorted(names - set(seen.get("seen", [])))
    for n in new:
        lines.append(f"NEW QUARANTINE (needs a human look): {n}")

    # persist state so each new quarantine is reported exactly once
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"updated": now, "seen": sorted(names)}, indent=1), encoding="utf-8")

    # watchdog: silence when nothing to act on
    if lines:
        print(f"quarantine audit {now[:10]} — {len(lines)} item(s):")
        for ln in lines:
            print(" " + ln)
        print("release action: fix/remove banner + retrieval_tier -> live, then `qmd update && qmd embed`")


if __name__ == "__main__":
    main()
