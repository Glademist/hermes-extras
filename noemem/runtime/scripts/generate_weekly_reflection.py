#!/usr/bin/env python3
"""Functional weekly project reflection generator (stable _current.md).

- Vždy udržuje jeden stabilní <project>_current.md (pro QMD i LLM)
- Před aktualizací archivuje předchozí obsah jako _weekly_....md
- _current.md se nikdy nepřejmenovává
"""

from pathlib import Path
from datetime import datetime
import sys
from typing import List, Optional


def has_meaningful_change(records: List[str]) -> bool:
    return len(records) > 0


def _build_reflection(project: str, records: List[str]) -> str:
    week = datetime.now().strftime("%Y-W%W")
    header = f"# Weekly Reflection — {project} ({week})\n\n"
    header += f"**Záznamů za týden:** {len(records)}\n\n"
    header += "## Změny a klíčové body\n\n"

    body = ""
    for i, rec in enumerate(records, 1):
        body += f"### Záznam {i}\n{rec.strip()}\n\n"

    footer = f"\n---\n*Generováno {datetime.now().isoformat()}*\n"
    return header + body + footer


def generate_tiered_weekly(project: str, records: List[str]) -> str:
    return _build_reflection(project, records)


def generate_multipass_weekly(project: str, records: List[str]) -> str:
    return _build_reflection(project, records)


def choose_mode(project: str, records: List[str]) -> str:
    total_chars = sum(len(r) for r in records)
    if total_chars > 8500 or len(records) >= 7:
        return "multipass"
    if project in {"hermes-noemem", "teacher", "xnotes"} and len(records) >= 5:
        return "multipass"
    return "tiered"


def generate_weekly_reflection(
    project: str, records: List[str], base_dir: Path
) -> Optional[Path]:
    """Hlavní entrypoint.

    - Vytvoří týdenní archivní kopii ze stávajícího _current.md (pokud existuje)
    - Přepíše _current.md novou týdenní reflexí
    - Vrací cestu k aktualizovanému _current.md nebo None
    """
    if not has_meaningful_change(records):
        return None

    mode = choose_mode(project, records)
    content = (
        generate_multipass_weekly(project, records)
        if mode == "multipass"
        else generate_tiered_weekly(project, records)
    )

    week = datetime.now().strftime("%Y-W%W")
    current_file = base_dir / f"{project}_current.md"
    weekly_archive = base_dir / f"{project}_weekly_{week}.md"

    # 1. Pokud _current.md existuje a není prázdný → archivujeme ho
    if current_file.exists() and current_file.stat().st_size > 0:
        current_file.replace(weekly_archive)

    # 2. Zápis nové reflexe přímo do stabilního _current.md
    current_file.write_text(content, encoding="utf-8")

    return current_file


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: generate_weekly_reflection.py <project> <records_file> <base_dir>")
        sys.exit(1)

    project = sys.argv[1]
    records_path = Path(sys.argv[2])
    base = Path(sys.argv[3])

    raw = records_path.read_text(encoding="utf-8").strip()
    records = [r.strip() for r in raw.split("---") if r.strip()]
    result = generate_weekly_reflection(project, records, base)
    print(result or "No changes — skipped")