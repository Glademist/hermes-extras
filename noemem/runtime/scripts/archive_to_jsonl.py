#!/usr/bin/env python3
"""archive_to_jsonl.py — převede hermes-noemem archiv na JSONL pro Leviathan.

Výstup: ~/hermes-noemem/runtime/data/archive.jsonl
Leviathan pak tento soubor indexuje pro fulltext search.

Použití:
  python3 archive_to_jsonl.py            # převést všech 712 archivních souborů
  python3 archive_to_jsonl.py --dry-run  # jen ukázka, nezapisovat
"""
import argparse
import json
import re
import sys
from pathlib import Path

ARCHIVE_DIR = Path.home() / "hermes-noemem" / "archive"
OUTPUT = Path.home() / "hermes-noemem" / "runtime" / "data" / "archive.jsonl"


def parse_archive_file(f: Path):
    """Převede jeden archivní soubor na JSONL záznam."""
    try:
        text = f.read_text(encoding="utf-8")
    except Exception:
        return None

    # Check for superseded banner (single line starting with > **[ZASTARALÉ)
    superseded = False
    superseded_by = ""
    if text.startswith("> **[ZASTARALÉ"):
        m_sup = re.match(
            r"^>\s*\*\*\[ZASTARALÉ\s*–\s*SUPERSEDED\s+by\s+([^\]]+)\]\*\*.*?\n\n(.*)",
            text,
            re.DOTALL,
        )
        if m_sup:
            superseded = True
            superseded_by = m_sup.group(1).strip()
            text = m_sup.group(2)

    text = text.lstrip()

    # Parse frontmatter
    m = re.match(r"^---\s*\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return None

    fm_text, body = m.group(1), m.group(2)
    fm = {}
    for line in fm_text.split("\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()

    # Parse body fields
    for key in ("TÉMA", "PROČ", "VÝSLEDEK", "PONAUČENÍ", "KATEGORIE", "PROJEKT"):
        m2 = re.search(rf"^{key}:\s*(.+)$", body, re.MULTILINE)
        if m2:
            fm[key.lower()] = m2.group(1).strip()

    fm["_body"] = body[:1000]
    fm["_file"] = f.name
    if superseded:
        fm["_superseded"] = True
        fm["_superseded_by"] = superseded_by

    return fm


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    all_files = sorted(ARCHIVE_DIR.glob("*.md"))
    count = 0
    superseded_count = 0
    errors = []

    if not args.dry_run:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with open(OUTPUT, "w", encoding="utf-8") if not args.dry_run else open("/dev/null", "w") as out:
        for f in all_files:
            rec = parse_archive_file(f)
            if rec:
                if not args.dry_run:
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                count += 1
                if rec.get("_superseded"):
                    superseded_count += 1
            else:
                errors.append(f.name)

    print(f"Converted {count}/{len(all_files)} files")
    print(f"Superseded: {superseded_count}")
    if errors:
        print(f"Errors: {len(errors)}")
        for e in errors[:5]:
            print(f"  {e}")
    if not args.dry_run:
        print(f"Output: {OUTPUT} ({OUTPUT.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
