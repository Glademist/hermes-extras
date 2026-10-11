#!/usr/bin/env python3
"""Memory file write API — všechny zápisy do ~/hermes-noemem/memory/*.md jdou přes tady.

Proč: nedatované stavové claimy v memory/ starly nenápadně a šířily se jako fakta
(incident "gemma :8096" žil v secretary.md po cutoveru na Nex 2026-09-14).
Tento skript vynucuje datování: agent předá jen text, skript přidá/aktualizuje
datum a provede validaci.

Použití:
  # Append bullet pod sekci (default):
  python3 memory_write.py append --file secretary.md --section "Živé endpointy" \
      --text "STT parakeet :8026, Nex :8096, embeddings :8082"

  # Nahradit existující řádek (fuzzy match na substring):
  python3 memory_write.py replace --file secretary.md --find "gemma \`:8096\`" \
      --text "Nex-N2.5-mini-MLX-oQ4 :8096 (od 2026-09-14)"

  # Pokud --text už neobsahuje datum (YYYY-MM-DD nebo "od YYYY-MM-DD"), skript ho doplní:
  #   "- <text> (ověřeno 2026-09-17)"

Exit codes: 0 = ok, 1 = usage/validate error, 2 = file/section/find not found.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

MEMORY_DIR = Path.home() / "hermes-noemem" / "memory"
DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def _target_path(file_arg: str) -> Path:
    p = Path(file_arg)
    if not p.suffix:
        p = p.with_suffix(".md")
    if not p.is_absolute():
        p = MEMORY_DIR / p
    if not p.exists():
        print(f"ERROR: soubor neexistuje: {p}", file=sys.stderr)
        raise SystemExit(2)
    if MEMORY_DIR not in p.parents:
        print(f"ERROR: pouze soubory v {MEMORY_DIR} jsou povolené", file=sys.stderr)
        raise SystemExit(1)
    return p


def _ensure_date(text: str) -> str:
    """Doplní ' (ověřeno YYYY-MM-DD)', pokud text už datum nemá."""
    if DATE_RE.search(text):
        return text
    today = date.today().isoformat()
    # Vždy jedna mezera před závorkou. Dřív se mezera pouštěla, když text končil
    # tečkou, a vzniklo "§9.(ověřeno …)" — což odporuje i docstringu výše
    # ("- <text> (ověřeno YYYY-MM-DD)") než typografii. (2026-10-01)
    return f"{text.rstrip()} (ověřeno {today})"


def _find_section_line(lines: list[str], section: str) -> int | None:
    """Vrátí index řádku nadpisu sekce (## Název), None pokud nenalezeno."""
    needle = section.strip().lower()
    for i, ln in enumerate(lines):
        m = re.match(r"^(#{1,4})\s+(.+?)\s*$", ln)
        if m and needle in m.group(2).lower():
            return i
    return None


def _section_end(lines: list[str], start: int) -> int:
    """Index konce sekce (další nadpis stejné nebo vyšší úrovně) — append před něj."""
    level_m = re.match(r"^(#+)", lines[start])
    level = len(level_m.group(1)) if level_m else 2
    for j in range(start + 1, len(lines)):
        m = re.match(r"^(#{1,4})\s+", lines[j])
        if m and len(m.group(1)) <= level:
            return j
    return len(lines)


def cmd_append(args) -> None:
    path = _target_path(args.file)
    lines = path.read_text(encoding="utf-8").splitlines()
    bullet = f"- {_ensure_date(args.text.strip())}"

    if args.section:
        idx = _find_section_line(lines, args.section)
        if idx is None:
            print(f"ERROR: sekce '{args.section}' nenalezena v {path.name}", file=sys.stderr)
            raise SystemExit(2)
        end = _section_end(lines, idx)
        # Vložit za poslední bullet / na konec sekce
        insert_at = end
        while insert_at > idx + 1 and not lines[insert_at - 1].startswith("-") and lines[insert_at - 1].strip() != "":
            insert_at -= 1
        lines.insert(insert_at, bullet)
    else:
        lines.append(bullet)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"OK append → {path.name}: {bullet[:100]}")


def cmd_replace(args) -> None:
    path = _target_path(args.file)
    lines = path.read_text(encoding="utf-8").splitlines()
    find = args.find
    hits = [i for i, ln in enumerate(lines) if find in ln]
    if not hits:
        print(f"ERROR: '{find}' nenalezeno v {path.name}", file=sys.stderr)
        raise SystemExit(2)
    if len(hits) > 1 and not args.replace_all:
        print(f"ERROR: '{find}' matches {len(hits)} řádků — specifikuj přesněji nebo --replace-all", file=sys.stderr)
        raise SystemExit(1)
    new_line = f"- {_ensure_date(args.text.strip())}" if args.as_bullet else _ensure_date(args.text.strip())
    for i in hits:
        old = lines[i]
        if args.as_bullet:
            # Zachovat indentaci původního bulletu
            indent_m = re.match(r"^(\s*)", old)
            indent = indent_m.group(1) if indent_m else ""
            lines[i] = indent + new_line
        else:
            lines[i] = new_line
        print(f"OK replace {path.name}:{i+1}")
        print(f"  - {old[:100]}")
        print(f"  + {lines[i][:100]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("append", help="přidat bullet (auto-datum)")
    a.add_argument("--file", required=True)
    a.add_argument("--section", default="", help="nadpis sekce, pod který appendovat")
    a.add_argument("--text", required=True)
    a.set_defaults(func=cmd_append)

    r = sub.add_parser("replace", help="nahradit řádek (auto-datum)")
    r.add_argument("--file", required=True)
    r.add_argument("--find", required=True, help="substring k nalezení")
    r.add_argument("--text", required=True)
    r.add_argument("--as-bullet", action="store_true", default=True)
    r.add_argument("--replace-all", action="store_true")
    r.set_defaults(func=cmd_replace)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
