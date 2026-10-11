#!/usr/bin/env python3
"""Nightly audit: nedatované stavové claimy v ~/hermes-noemem/memory/*.md.

Proč: nedatovaný stavový řádek ("gemma :8096") zůstává po cutoveru čten jako
aktuální fakt (incident 2026-09-17). Tento audit je najde, označí komentářem
<!-- UNDATED-STATE nalezeno YYYY-MM-DD --> a zaloguje počet pro tracking.

Kritérium je obsahové, ne časové: řádek zní stavově (state-hint slova) a nemá
žádné datum (YYYY-MM-DD). Audit NIKDY nedoplňuje platnost — jen značí nález.

Použití:
  python3 audit_memory_dates.py            # report only (stdout + log)
  python3 audit_memory_dates.py --fix      # vložit UNDATED-STATE značky do souborů
  python3 audit_memory_dates.py --clear    # odstranit vyřešené značky (řádek už má datum)

Exit 0 vždy (fail-open, spouští se z nightly).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

MEMORY_DIR = Path.home() / "hermes-noemem" / "memory"
LOG_DIR = Path.home() / "hermes-noemem" / "logs"
BASELINE = LOG_DIR / "memory-audit-baseline.json"
MARKER = "<!-- UNDATED-STATE nalezeno {day}: -->"

DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
STATE_HINTS = (
    "běží", "produkce", "produkční", "aktuální", "živ", "aktivní", "nasazen",
    "default", "production", "runs", "live", "endpoint", "port", "stávající",
    "primární", "hlavní", "současn",
)
# Řádky, které nikdy neflagovat (pravidla/konvence/reference, ne stav systému)
SKIP_PATTERNS = (
    re.compile(r"^\s*[-*]?\s*(Prefer|Nikdy|Vždy|Nepoužívat|Používej| Obtain |Model )", re.I),
    re.compile(r"^\s*#"),                      # nadpisy
    re.compile(r"UNDATED-STATE"),              # vlastní značky
    re.compile(r"^\s*$"),
    re.compile(r"^---\s*$"),                   # frontmatter delimiter
    re.compile(r"^\s*\w+:"),                   # frontmatter klíče
)


def is_state_line(line: str) -> bool:
    if any(p.match(line) for p in SKIP_PATTERNS):
        return False
    if DATE_RE.search(line):
        return False
    low = line.lower()
    return any(h in low for h in STATE_HINTS)


def scan() -> dict[str, list[tuple[int, str]]]:
    findings: dict[str, list[tuple[int, str]]] = {}
    for f in sorted(MEMORY_DIR.glob("*.md")):
        hits = []
        for i, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if is_state_line(ln):
                hits.append((i, ln.strip()[:120]))
        if hits:
            findings[f.name] = hits
    return findings


def apply_fix(findings: dict) -> int:
    today = date.today().isoformat()
    n = 0
    for fname, hits in findings.items():
        path = MEMORY_DIR / fname
        lines = path.read_text(encoding="utf-8").splitlines()
        for ln_no, _ in hits:
            idx = ln_no - 1
            indent_m = re.match(r"^(\s*)", lines[idx])
            indent = indent_m.group(1) if indent_m else ""
            marker = indent + MARKER.format(day=today)
            lines.insert(idx, marker)
            n += 1
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return n


def clear_resolved() -> int:
    """Odstranit značky nad řádky, které teď už datum mají (opraveno)."""
    n = 0
    for f in sorted(MEMORY_DIR.glob("*.md")):
        lines = f.read_text(encoding="utf-8").splitlines()
        out = []
        for i, ln in enumerate(lines):
            m = re.match(r"^\s*<!-- UNDATED-STATE nalezeno (\d{4}-\d{2}-\d{2}): -->\s*$", ln)
            if m and i + 1 < len(lines) and DATE_RE.search(lines[i + 1]):
                n += 1
                continue
            out.append(ln)
        if n and out != lines:
            f.write_text("\n".join(out) + "\n", encoding="utf-8")
    return n


def write_metrics(findings: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    total = sum(len(v) for v in findings.values())
    log = LOG_DIR / f"memory-audit-{today}.log"
    with open(log, "w", encoding="utf-8") as lf:
        lf.write(f"memory audit {today}\nundated_state_lines: {total}\n")
        for fname, hits in findings.items():
            lf.write(f"{fname}: {len(hits)}\n")
            for ln_no, txt in hits:
                lf.write(f"  :{ln_no} {txt}\n")
    # Baseline/progres tracking (append jeden záznam za den)
    data = {}
    if BASELINE.exists():
        try:
            data = json.loads(BASELINE.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
    data[today] = {"undated_state_lines": total, "files": {k: len(v) for k, v in findings.items()}}
    BASELINE.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"undated_state_lines: {total} (log: {log.name})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="vložit UNDATED-STATE značky")
    ap.add_argument("--clear", action="store_true", help="odstranit značky nad opravenými řádky")
    args = ap.parse_args()

    if args.clear:
        print(f"cleared: {clear_resolved()} markers")
        return

    findings = scan()
    for fname, hits in findings.items():
        print(f"\n{fname} ({len(hits)}):")
        for ln_no, txt in hits:
            print(f"  :{ln_no} {txt}")
    if args.fix:
        print(f"\ninserted {apply_fix(findings)} markers")
    write_metrics(findings)


if __name__ == "__main__":
    main()
