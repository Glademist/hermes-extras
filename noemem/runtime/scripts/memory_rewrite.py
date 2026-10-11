#!/usr/bin/env python3
"""memory_rewrite.py — sanctioned FULL-FILE rewrite of a memory/*.md topic file.

Why: `memory_write.py` can only append/replace single bullets, but repairs need a
whole-file write — a corrupted file (raw reasoning text), pruning history out of a
"now" file, removing duplicated sections. This is that API, with the same
guarantees: dated backup next to the file's history, `<!-- last consolidated -->`
stamp, and the merge validator from `memory_consolidate` (rejects reasoning /
planning text and a lost file skeleton).

Usage:
  python3 memory_rewrite.py --file infra.md --from-file /tmp/infra.new.md [--dry-run]
"""
import argparse
import datetime
import shutil
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR.parent))
sys.path.insert(0, str(_SCRIPTS_DIR))
from noemem_paths import NOEMEM_ROOT  # noqa: E402
from memory_consolidate import budget_ok, merge_output_ok  # noqa: E402

MEMORY_DIR = NOEMEM_ROOT / "memory"
BACKUP_DIR = NOEMEM_ROOT / "backups" / "memory"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="memory file name, e.g. infra.md")
    ap.add_argument("--from-file", required=True, help="path to the new full content")
    ap.add_argument("--create", action="store_true", help="create the file if missing")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    target = MEMORY_DIR / args.file
    if not target.exists() and not args.create:
        print(f"REFUSED: no such memory file: {target} (use --create)")
        return 1
    new = Path(args.from_file).read_text(encoding="utf-8").strip()
    ok, why = merge_output_ok(new)
    if not ok:
        print(f"REFUSED: {args.file}: {why}")
        return 1
    old = target.read_text(encoding="utf-8") if target.exists() else ""
    ok, why = budget_ok(new, old)
    if not ok:
        print(f"REFUSED: {args.file}: {why}")
        return 1
    if args.dry_run:
        print(f"dry-run ok: {args.file} would be written ({len(new)} chars)")
        return 0

    if target.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        bak = BACKUP_DIR / f"{args.file}.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(target, bak)
        print(f"backup {bak}")
    # Drop any stamp the caller passed through --from-file, so a rewrite is idempotent.
    # Without this the stamp is stacked on the previous one on every run, and three
    # files ended up with two (2026-10-05).
    new = "\n".join(l for l in new.splitlines()
                    if not l.strip().startswith("<!-- last consolidated")).strip()

    stamp = (
        "<!-- last consolidated "
        f"{datetime.datetime.now().isoformat(timespec='minutes')} (memory_rewrite) -->\n\n"
    )
    target.write_text(stamp + new + "\n", encoding="utf-8")
    print(f"rewrote {args.file} ({len(new)} chars, previous {len(old)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())