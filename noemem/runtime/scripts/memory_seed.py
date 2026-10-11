#!/usr/bin/env python3
"""memory_seed.py — one-time Phase 1 seed of ~/hermes-noemem/memory/ from the archive.

Anthropic-alignment plan Phase 1 (plans/2026-07-10_anthropic-alignment.md).

Strategy for the slow local model:
  1. Select live (non-superseded) archives that pass archive_lint.
  2. Group by domain (project slug -> memory file), batch N archives per extract call.
  3. Extract durable facts (English) per batch; ephemeral details are dropped.
  4. One write call per memory file composes the final markdown.
  5. State saved after every LLM call -> fully resumable (just re-run).

Usage:
  python3 memory_seed.py            # run / resume
  python3 memory_seed.py --status   # show progress, no LLM
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR.parent))
sys.path.insert(0, str(_SCRIPTS_DIR))
from noemem_paths import ARCHIVE_DIR, NOEMEM_ROOT, STATE_DIR  # noqa: E402
from archive_lint import lint_file, load_catalog_slugs, parse_frontmatter  # noqa: E402
from dreamer_v2 import call_llm  # noqa: E402  (reuses api_lock + mtplx_client)

MEMORY_DIR = NOEMEM_ROOT / "memory"
STATE_FILE = STATE_DIR / "memory_seed_state.json"
BATCH_SIZE = 6
EXTRACT_MAX_TOKENS = 1800
WRITE_MAX_TOKENS = 3000

# project slug (canonical, lowercase) -> memory file
DOMAIN_MAP = {
    "local-api": "infra.md",
    "mtplx-testing-notes": "infra.md",
    "thermalforge": "infra.md",
    "security": "infra.md",
    "hermes-noemem": "hermes-system.md",
    "hermes-gateway-setup": "hermes-system.md",
    "hermes-cron-watchdogs": "hermes-system.md",
    "qmd-memory-plugin": "hermes-system.md",
    "clean-scrape": "hermes-system.md",
    "teacher": "teacher.md",
    "xnotes": "projects.md",
}
FALLBACK_BY_CATEGORY = {"personal": "user-profile.md"}
DEFAULT_FILE = "projects.md"

# `personal` used to be the fallback for ANY record whose project was not in
# DOMAIN_MAP. Because the category enum is 3/4 inert, that parked infrastructure
# in user-profile.md — by 2026-09-30 over 60% of the file was ports, launchd and
# model inventory, including a stale "Darkbloom available" claim. The user's
# profile is written by hand and by seed only from records that are genuinely
# about him; an unmapped project is NOT evidence of that. (2026-09-30)

FILE_DESCRIPTIONS = {
    "infra.md": "Local inference infrastructure: MTPLX server, models, benchmarks, launchd services, API locking, ThermalForge cooling, supply-chain security.",
    "hermes-system.md": "Hermes agent system: hermes-noemem memory pipeline (dreamer, nightly, QMD), gateway/providers, cron jobs and watchdogs, skills tooling.",
    "teacher.md": "Teacher project: ZŠ study-material generation pipeline (kanban, assemblies, watchdogs, quality checks).",
    "user-profile.md": "Durable facts about the user the user: biography, preferences, working style, family, interests.",
    "projects.md": "Smaller / inactive projects: ExampleProject and one-off efforts.",
    "conventions.md": "Cross-cutting rules and safe patterns the agent must follow (shell env, cron patterns, coding conventions, known pitfalls).",
}

EXTRACT_PROMPT = """You are building a durable memory file from session archive records.

Below are {n} archive records (Czech, fields TÉMA/PROČ/VÝSLEDEK/PONAUČENÍ). Extract DURABLE facts in ENGLISH.

DURABLE = architecture, file paths, commands, configuration, rules, decisions that still matter, user preferences.
EPHEMERAL = job ids, one-off sizes/dates, "was fixed once" details with no lasting rule -> DROP these.

Output format (nothing else):
FACTS:
- fact sentence (specific: keep exact paths, ports, commands in `backticks`)
CONVENTIONS:
- cross-cutting rule the agent must always follow (or "none")
VERDICTS:
- one line `X → verdict + why` ONLY for a durable trial-and-verdict: we tried something and it was abandoned, rejected, or kept for a reason that will still matter in a year ("27B locally → not worth it: an hour instead of a minute for ~5% better quality"). No current status, no ports/paths needed. BIG THINGS ONLY: never a one-off event, never an approval-gate denial / tool error / retry, never "today X happened". Usually "none".

Max 4 facts per record. If a newer record contradicts an older one, keep only the newer fact.

=== RECORDS ===
{records}"""

WRITE_PROMPT = """You are writing `memory/{fname}` — a durable memory file for an AI agent.

Scope: {description}

Compose a clean English markdown file from the fact list below.
Rules:
- Group facts into `## <topic>` sections (3-8 sections).
- Merge duplicates; if facts conflict, prefer the one marked with a later date.
- Keep exact paths/commands/ports in `backticks`. No invented content — only the facts given.
- Terse bullet style. No preamble, no conclusion. Start directly with `# {title}`.

=== FACTS ===
{facts}"""


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"batches": {}, "written": []}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


def target_file(fm: dict, catalog: set) -> str:
    """Route an archive to a memory file.

    Order: canonical project -> category -> default. The category branch used to
    send `personal` to user-profile.md for ANY project missing from DOMAIN_MAP.
    Since the enum is 3/4 inert, that is not a signal that a record is about the
    user — it only means the project is unmapped. Infrastructure therefore landed
    in the user profile. `personal` now requires positive evidence: the record
    must have no project at all. Everything unmapped goes to DEFAULT_FILE, which
    is where a one-off effort belongs anyway. (2026-09-30)
    """
    proj = (fm.get("project") or "").strip().lower()
    proj = re.sub(r"-\d{8}(-\d{8})?$", "", proj)
    if proj in DOMAIN_MAP:
        return DOMAIN_MAP[proj]
    cat = (fm.get("category") or "").strip().lower()
    # project present but unmapped -> not evidence of a personal record
    if not proj and cat in FALLBACK_BY_CATEGORY:
        return FALLBACK_BY_CATEGORY[cat]
    return DEFAULT_FILE


def record_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    body = re.split(r"\n---\s*\n", text, maxsplit=1)[-1]
    return f"--- {path.name} ---\n{body.strip()[:3500]}"


def select_archives() -> dict:
    """Return {memory_file: [paths]} for live, lint-clean archives."""
    catalog = load_catalog_slugs()
    groups: dict = {}
    skipped = 0
    for path in sorted(ARCHIVE_DIR.glob("*.md")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(text) or {}
        if fm.get("retrieval_tier") in ("superseded", "quarantine"):
            continue
        if lint_file(path, catalog)["errors"]:
            skipped += 1
            continue
        groups.setdefault(target_file(fm, catalog), []).append(path)
    print(f"selected: {sum(len(v) for v in groups.values())} live+clean archives, {skipped} skipped (lint errors)")
    return groups


def parse_extract(output: str) -> tuple[list[str], list[str]]:
    facts, convs = [], []
    bucket = None
    for line in (output or "").splitlines():
        s = line.strip()
        if s.upper().startswith("FACTS"):
            bucket = facts
        elif s.upper().startswith("CONVENTIONS"):
            bucket = convs
        elif s.startswith(("-", "*")) and bucket is not None:
            item = s.lstrip("-* ").strip()
            if item and item.lower() != "none":
                bucket.append(item)
    return facts, convs


def parse_verdicts(output: str) -> list[str]:
    """VERDICTS: section of an extract output -> durable trial-and-verdict lines."""
    out: list[str] = []
    bucket = False
    for line in (output or "").splitlines():
        s = line.strip()
        if s.upper().startswith(("FACTS", "CONVENTIONS", "VERDICTS")):
            bucket = s.upper().startswith("VERDICTS")
        elif s.startswith(("-", "*")) and bucket:
            item = s.lstrip("-* ").strip()
            if item and item.lower() != "none" and len(item) > 25:
                out.append(item)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    state = load_state()
    groups = select_archives()

    batches = []  # (batch_key, memory_file, [paths])
    for fname, paths in sorted(groups.items()):
        for i in range(0, len(paths), BATCH_SIZE):
            chunk = paths[i:i + BATCH_SIZE]
            key = f"{fname}:{i // BATCH_SIZE}"
            batches.append((key, fname, chunk))

    done = sum(1 for k, _, _ in batches if k in state["batches"])
    print(f"batches: {done}/{len(batches)} extracted | files written: {len(state['written'])}")
    if args.status:
        for k, f, c in batches:
            mark = "✓" if k in state["batches"] else "·"
            print(f"  {mark} {k} ({len(c)} archives)")
        return

    # Pass 1: extract per batch
    for key, fname, chunk in batches:
        if key in state["batches"]:
            continue
        print(f"\n▶ extract {key} ({len(chunk)} archives)")
        records = "\n\n".join(record_text(p) for p in chunk)
        out = call_llm(
            EXTRACT_PROMPT.format(n=len(chunk), records=records),
            system="You are a precise fact extractor. Output only the requested format, in English.",
            max_tokens=EXTRACT_MAX_TOKENS,
            enable_thinking=False,
        )
        if not out:
            print(f"  ❌ LLM failed for {key}; rerun script to retry")
            sys.exit(1)
        facts, convs = parse_extract(out)
        print(f"  {len(facts)} facts, {len(convs)} conventions")
        state["batches"][key] = {"facts": facts, "conventions": convs, "files": [p.name for p in chunk]}
        save_state(state)

    # Pass 2: write memory files
    MEMORY_DIR.mkdir(exist_ok=True)
    all_convs: list[str] = []
    for fname in sorted(set(f for _, f, _ in batches)):
        facts = []
        for key, bf, _ in batches:
            if bf == fname and key in state["batches"]:
                facts.extend(state["batches"][key]["facts"])
                all_convs.extend(state["batches"][key]["conventions"])
        if fname in state["written"] or not facts:
            continue
        print(f"\n▶ write memory/{fname} ({len(facts)} facts)")
        title = fname.removesuffix(".md").replace("-", " ").title()
        out = call_llm(
            WRITE_PROMPT.format(
                fname=fname,
                title=title,
                description=FILE_DESCRIPTIONS.get(fname, ""),
                facts="\n".join(f"- {f}" for f in facts),
            ),
            system="You are a technical writer maintaining agent memory files. English only.",
            max_tokens=WRITE_MAX_TOKENS,
            enable_thinking=False,
        )
        if not out:
            print(f"  ❌ LLM failed writing {fname}; rerun to retry")
            sys.exit(1)
        header = f"<!-- seeded from archive {date.today().isoformat()} | review before trusting -->\n\n"
        (MEMORY_DIR / fname).write_text(header + out.strip() + "\n", encoding="utf-8")
        state["written"].append(fname)
        save_state(state)
        print(f"  ✅ memory/{fname}")

    # conventions.md from accumulated conventions (single write call)
    if "conventions.md" not in state["written"] and all_convs:
        uniq = list(dict.fromkeys(all_convs))
        print(f"\n▶ write memory/conventions.md ({len(uniq)} conventions)")
        out = call_llm(
            WRITE_PROMPT.format(
                fname="conventions.md",
                title="Conventions",
                description=FILE_DESCRIPTIONS["conventions.md"],
                facts="\n".join(f"- {c}" for c in uniq),
            ),
            system="You are a technical writer maintaining agent memory files. English only.",
            max_tokens=WRITE_MAX_TOKENS,
            enable_thinking=False,
        )
        if out:
            header = f"<!-- seeded from archive {date.today().isoformat()} | review before trusting -->\n\n"
            (MEMORY_DIR / "conventions.md").write_text(header + out.strip() + "\n", encoding="utf-8")
            state["written"].append("conventions.md")
            save_state(state)
            print("  ✅ memory/conventions.md")

    print(f"\n=== seed done: {len(state['written'])} memory files in {MEMORY_DIR} ===")
    print("Review the files, then: cd ~/hermes-noemem && git add memory && git commit")


if __name__ == "__main__":
    main()
