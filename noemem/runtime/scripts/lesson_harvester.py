#!/usr/bin/env python3
"""lesson_harvester.py — Phase 3a: distill PONAUČENÍ fields into a review queue.

Closes the procedural-memory loop: archive lessons -> deduplicated English
proposals in agent/lesson-queue.md -> user approves on Discord -> agent
patches skills / memory. NO automatic skill patching here.

State keeps processed archive names, so each archive is harvested once.

Usage:
  python3 lesson_harvester.py               # last 7 days of unprocessed archives
  python3 lesson_harvester.py --days 30
  python3 lesson_harvester.py --all         # full backlog (batched)
  python3 lesson_harvester.py --dry-run     # show input lessons, no LLM
"""
import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR.parent))
sys.path.insert(0, str(_SCRIPTS_DIR))
from noemem_paths import ARCHIVE_DIR, AGENT_DIR, STATE_DIR  # noqa: E402
from archive_lint import lint_file, load_catalog_slugs, parse_frontmatter  # noqa: E402
from dreamer_v2 import call_llm  # noqa: E402

QUEUE_FILE = AGENT_DIR / "lesson-queue.md"
STATE_FILE = STATE_DIR / "lesson_harvester_state.json"
BATCH_SIZE = 20
HARVEST_MAX_TOKENS = 1600

HARVEST_PROMPT = """You review "lessons learned" extracted from an AI agent's session archives (Czech).
Distill them into actionable proposals for the agent's procedural memory.

For each lesson decide:
- DROP if: trivial, one-off with no lasting rule, vague truism ("be careful"), or duplicate of another lesson in this batch.
- Otherwise output a proposal block:

LESSON: <one-line imperative rule in English, specific (keep exact paths/commands/ports in `backticks`)>
ACTION: <skill | memory>   (skill = reusable procedure worth a skill patch; memory = durable fact for memory/*.md)
TARGET: <suggested skill name or memory file, or "new-skill: <slug>">
SOURCE: <archive filenames, comma-separated>
CONFIDENCE: <high | medium | low>  (high = lesson repeats or clearly generalizes)

Output only proposal blocks separated by blank lines. If nothing survives, output exactly: NO PROPOSALS

=== LESSONS ===
{lessons}"""


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"processed": []}


def extract_lesson(text: str) -> str:
    m = re.search(r"^PONAUČENÍ:[ \t]*(\S.*?)(?=^\S+:|\Z)", text, re.MULTILINE | re.DOTALL)
    if not m:
        return ""
    lesson = " ".join(m.group(1).split())
    # drop empty / placeholder lessons ("...", "N/A", lone punctuation)
    if len(re.sub(r"[^\wá-žÁ-Ž]", "", lesson)) < 15:
        return ""
    return lesson


def select_archives(days: int, harvest_all: bool, processed: set) -> list[Path]:
    catalog = load_catalog_slugs()
    cutoff = "" if harvest_all else (date.today() - timedelta(days=days)).isoformat()
    out = []
    for path in sorted(ARCHIVE_DIR.glob("*.md")):
        if path.name in processed or path.name[:10] < cutoff:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(text) or {}
        if fm.get("retrieval_tier") in ("superseded", "quarantine"):
            continue
        if lint_file(path, catalog)["errors"]:
            continue
        if extract_lesson(text):
            out.append(path)
    return out


def lesson_line(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(text) or {}
    return (f"[{path.name} | project: {fm.get('project', '?')} | "
            f"category: {fm.get('category', '?')}]\n{extract_lesson(text)}")


def append_queue(proposals: str, batch_paths: list[Path]):
    AGENT_DIR.mkdir(exist_ok=True)
    header = "" if QUEUE_FILE.exists() else (
        "# Lesson queue\n\n"
        "Proposals distilled from archive PONAUČENÍ fields (lesson_harvester.py).\n"
        "Review flow: user approves on Discord -> agent patches skill/memory -> "
        "move block to `## Done` (or delete if rejected).\n\n## Pending\n"
    )
    stamp = f"\n<!-- harvested {date.today().isoformat()}: {len(batch_paths)} archives -->\n"
    with QUEUE_FILE.open("a", encoding="utf-8") as f:
        f.write(header + stamp + proposals.strip() + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    state = load_state()
    archives = select_archives(args.days, args.all, set(state["processed"]))
    if not archives:
        print("harvester: no new archives with lessons, nothing to do")
        return
    print(f"harvester: {len(archives)} archives with lessons")

    if args.dry_run:
        for p in archives:
            print(f"\n--- {p.name}\n{extract_lesson(p.read_text(encoding='utf-8', errors='replace'))}")
        return

    new_blocks = 0
    for i in range(0, len(archives), BATCH_SIZE):
        batch = archives[i:i + BATCH_SIZE]
        out = call_llm(
            HARVEST_PROMPT.format(lessons="\n\n".join(lesson_line(p) for p in batch)),
            system="You are a strict curator of procedural memory. English only. Output only the requested format.",
            max_tokens=HARVEST_MAX_TOKENS,
            enable_thinking=False,
        )
        if not out:
            print(f"  ❌ batch {i // BATCH_SIZE + 1}: LLM failed; stopping (state saved, re-run resumes)")
            break
        out = out.strip()
        if out != "NO PROPOSALS" and "LESSON:" in out:
            append_queue(out, batch)
            n = out.count("LESSON:")
            new_blocks += n
            print(f"  batch {i // BATCH_SIZE + 1}: {n} proposals from {len(batch)} lessons")
        else:
            print(f"  batch {i // BATCH_SIZE + 1}: no proposals survived")
        state["processed"].extend(p.name for p in batch)
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"harvester done: {new_blocks} new proposals in {QUEUE_FILE}")


if __name__ == "__main__":
    main()
