#!/usr/bin/env python3
"""archive_verify.py — Phase 2 verify step (dreaming: Verify).

Lints today's new archives; failures get `retrieval_tier: quarantine` in
frontmatter so they are excluded from QMD retrieval, nightly reflection and
memory consolidation. No LLM. Regeneration is manual:
  python3 dreamer_v2.py --session-id <source_session_id>

Usage:
  python3 archive_verify.py            # verify archives dated today
  python3 archive_verify.py --date 2026-07-09
"""
import argparse
import re
import sys
from datetime import date
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR.parent))
sys.path.insert(0, str(_SCRIPTS_DIR))
from noemem_paths import ARCHIVE_DIR  # noqa: E402
from archive_lint import lint_file, load_catalog_slugs  # noqa: E402


BANNER = "> **[QUARANTINE — lint failed]** Záznam obsahuje poškozená fakta (rozbité cesty/kotvy). Nepoužívej jako zdroj; regeneruj přes dreamer_v2 --session-id.\n\n"


def quarantine(path: Path) -> bool:
    """Set retrieval_tier: quarantine in frontmatter + warning banner. Returns True if changed."""
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^---\s*\n(.*?)\n---", text, re.DOTALL | re.MULTILINE)
    if not m:
        # no frontmatter at all — prepend a minimal one
        path.write_text(f"{BANNER}---\nretrieval_tier: quarantine\n---\n\n{text}", encoding="utf-8")
        return True
    fm = m.group(1)
    if re.search(r"^retrieval_tier:\s*quarantine\s*$", fm, re.M):
        return False
    if re.search(r"^retrieval_tier:", fm, re.M):
        new_fm = re.sub(r"^retrieval_tier:.*$", "retrieval_tier: quarantine", fm, flags=re.M)
    else:
        new_fm = fm + "\nretrieval_tier: quarantine"
    new_text = text.replace(m.group(0), f"---\n{new_fm}\n---", 1)
    if not new_text.startswith(">"):
        new_text = BANNER + new_text
    path.write_text(new_text, encoding="utf-8")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    catalog = load_catalog_slugs()
    targets = sorted(ARCHIVE_DIR.glob(f"{args.date}_*.md"))
    if not targets:
        print(f"verify: no archives dated {args.date}")
        return

    quarantined = 0
    for path in targets:
        r = lint_file(path, catalog)
        if r["errors"]:
            if quarantine(path):
                quarantined += 1
            print(f"⛔ QUARANTINED {path.name}")
            for e in r["errors"]:
                print(f"     {e}")
        else:
            print(f"✓ {path.name}")

    print(f"verify: {len(targets)} archives, {quarantined} quarantined")
    # exit 0 always — quarantine is handled, not a pipeline failure
    # (healthcheck reads the QUARANTINED lines from the cron log)


if __name__ == "__main__":
    main()
