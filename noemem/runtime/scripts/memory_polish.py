#!/usr/bin/env python3
"""memory_polish.py — Phase 2 polish step (dreaming: Polish). Weekly.

Checks memory/ files for staleness and contradictions against ground truth:
  1. Code checks (no LLM): teacher facts from disk registries; dead ports;
     files over size cap; quarantined archives older than 14 days (report).
  2. One LLM call per memory file (budget ≤6): find duplicate bullets and
     contradictions, propose fixes.
Meaning-changing proposals go to agent/polish-proposals.md for approval —
nothing is rewritten autonomously except pure duplicates.

Usage:
  python3 memory_polish.py            # full run
  python3 memory_polish.py --code-only  # skip LLM pass
"""
import argparse
import json
import re
import socket
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR.parent))
sys.path.insert(0, str(_SCRIPTS_DIR))
from noemem_paths import ARCHIVE_DIR, NOEMEM_ROOT  # noqa: E402
from archive_lint import parse_frontmatter  # noqa: E402
from dreamer_v2 import call_llm, get_teacher_facts  # noqa: E402

MEMORY_DIR = NOEMEM_ROOT / "memory"
PROPOSALS_FILE = NOEMEM_ROOT / "agent" / "polish-proposals.md"
GOALS_FILE = NOEMEM_ROOT / "personal" / "_goals.md"
# Single source of truth for the memory budget. Was hardcoded 14000 here while
# memory_consolidate.budget_ok() and noemem_selfcheck.py both used 6000, so a
# file at e.g. 8000 chars was "fine" for the weekly polish audit, refused growth
# by consolidation, and alerted the daily selfcheck at the same time (2026-10-01).
from memory_consolidate import FILE_CHAR_CAP, FILE_CHAR_TARGET  # noqa: E402

POLISH_PROMPT = """You are auditing `memory/{fname}`, a durable memory file for an AI agent.

GROUND TRUTH (authoritative, from disk/user — overrides the file):
{ground_truth}

Audit the file below. Report ONLY concrete problems, using this exact format:

DUPLICATE:
- quote of bullet A ~ quote of bullet B (safe to merge)
CONTRADICTION:
- quote of file claim ~ what ground truth says instead
STALE:
- quote of claim that describes a past/removed state as current
(sections may be empty — write "none")

Do not rewrite the file. Do not invent problems. Max 5 items per section.

=== memory/{fname} ===
{content}"""


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def code_checks() -> list[str]:
    """Pure-code findings, no LLM."""
    findings = []
    # size: the SOFT target is the trim signal, the hard cap is the alarm
    for f in sorted(MEMORY_DIR.glob("*.md")):
        size = len(f.read_text(encoding="utf-8"))
        if size > FILE_CHAR_CAP:
            findings.append(f"SIZE: memory/{f.name} is {size} chars (> hard cap {FILE_CHAR_CAP}) — split the topic")
        elif size > FILE_CHAR_TARGET:
            findings.append(f"SIZE: memory/{f.name} is {size} chars (> soft target {FILE_CHAR_TARGET}) — trim verbosity, merge duplicates")
    # port claims vs reality
    blob = "\n".join(f.read_text(encoding="utf-8") for f in MEMORY_DIR.glob("*.md"))
    for port in sorted({int(m) for m in re.findall(r"port[` ]*(\d{4,5})", blob, re.I) if 1024 <= int(m) <= 65535}):
        state = "open" if port_open(port) else "CLOSED"
        if state == "CLOSED":
            findings.append(f"PORT: memory files mention port {port}, which is not listening now (verify claim currency)")
    # old quarantined archives
    cutoff = date.today() - timedelta(days=14)
    for f in sorted(ARCHIVE_DIR.glob("*.md")):
        fm = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace")) or {}
        if fm.get("retrieval_tier") == "quarantine":
            m = re.match(r"^(\d{4}-\d{2}-\d{2})", f.name)
            if m and date.fromisoformat(m.group(1)) < cutoff:
                findings.append(f"QUARANTINE: {f.name} quarantined >14d — regenerate or delete")
    return findings


def ground_truth_for(fname: str) -> str:
    parts = []
    if fname == "teacher.md":
        facts = get_teacher_facts()
        if facts:
            parts.append("Teacher subjects (from disk registries, code-computed):\n" + facts)
    if GOALS_FILE.exists():
        g = GOALS_FILE.read_text(encoding="utf-8")
        m = re.search(r"## Active now.*?(?=\n## |\Z)", g, re.DOTALL)
        if m:
            parts.append("_goals.md Active now (user-owned):\n" + m.group(0)[:1500])
    return "\n\n".join(parts) or "(no specific ground truth for this file — check internal consistency only)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code-only", action="store_true")
    args = ap.parse_args()

    findings = code_checks()
    llm_reports: dict[str, str] = {}

    if not args.code_only:
        # Audit the BIGGEST files first, not the first 6 alphabetically: this pass
        # exists to reduce detail, so its budget goes where the bloat is. A detail
        # file that is never audited never shrinks.
        files = sorted(MEMORY_DIR.glob("*.md"),
                       key=lambda f: len(f.read_text(encoding="utf-8")), reverse=True)[:8]
        for f in files:
            content = f.read_text(encoding="utf-8")
            content = "\n".join(l for l in content.splitlines() if not l.startswith("<!--"))
            out = call_llm(
                POLISH_PROMPT.format(fname=f.name, ground_truth=ground_truth_for(f.name), content=content[:12000]),
                system="You are a precise auditor. English. Only the requested format.",
                max_tokens=1200,
                enable_thinking=False,
            )
            if out and not re.fullmatch(r"\s*(DUPLICATE:\s*-?\s*none\s*CONTRADICTION:\s*-?\s*none\s*STALE:\s*-?\s*none\s*)", out.strip(), re.I):
                has_content = any(
                    line.strip().startswith("-") and "none" not in line.lower()[:10]
                    for line in out.splitlines()
                )
                if has_content:
                    llm_reports[f.name] = out.strip()
            print(f"  audited {f.name}: {'issues found' if f.name in llm_reports else 'clean'}")

    if not findings and not llm_reports:
        print("polish: no findings — memory/ is clean")
        return

    PROPOSALS_FILE.parent.mkdir(exist_ok=True)
    stamp = datetime.now().isoformat(timespec="minutes")
    lines = [f"# Polish proposals — {stamp}", ""]
    lines.append("*(Weekly memory audit. Approve items and the agent applies them; nothing below was changed automatically.)*\n")
    if findings:
        lines.append("## Code checks")
        lines += [f"- {x}" for x in findings]
        lines.append("")
    for fname, report in llm_reports.items():
        lines.append(f"## memory/{fname}")
        lines.append(report)
        lines.append("")
    PROPOSALS_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"polish: {len(findings)} code findings, {len(llm_reports)} files with LLM findings → {PROPOSALS_FILE}")


if __name__ == "__main__":
    main()
