#!/usr/bin/env python3
"""generate_operating_context.py — Phase 3b: build agent/operating-context.md.

Single entry point for a live agent session: what is active now, where the
memory layers live and which wins. Deterministic (no LLM):
  - "Active now" section copied verbatim from personal/_goals.md
  - pointers to memory/*.md with first heading + size
  - hard rules section maintained by hand between markers (preserved on rebuild)

Runs nightly after consolidate; also safe to run ad hoc.
"""
import re
import sys
from datetime import datetime
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR.parent))
from noemem_paths import AGENT_DIR, GOALS_FILE, NOEMEM_ROOT  # noqa: E402

MEMORY_DIR = NOEMEM_ROOT / "memory"
OUT_FILE = AGENT_DIR / "operating-context.md"
RULES_BEGIN = "<!-- HARD-RULES:BEGIN (hand-maintained, survives rebuild) -->"
RULES_END = "<!-- HARD-RULES:END -->"
DEFAULT_RULES = """- Memory precedence: `personal/_goals.md` (user authority) > `memory/*.md` (durable) > `projekty/*-current.md` (recent synth) > archive (history).
- Never edit `personal/_goals.md`; propose changes in chat instead.
- Living files (`projekty/*-current.md`, `agent/_goals-agent.md`) are written by pipelines — do not hand-patch them.
- Before working on a project slug, read its `projekty/<slug>-current.md` and the matching `memory/` file."""


def active_now_section(goals_text: str) -> str:
    m = re.search(r"^## Active now.*?\n(.*?)(?=^#|\Z)", goals_text, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else "(section '## Active now' not found in _goals.md)"


def memory_pointers() -> str:
    lines = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "README.md":
            continue
        first = next((l.lstrip("# ").strip() for l in f.read_text(encoding="utf-8").splitlines()
                      if l.startswith("# ")), f.stem)
        lines.append(f"- `memory/{f.name}` — {first} ({f.stat().st_size // 1024} KB)")
    return "\n".join(lines) or "(no memory files)"


def existing_rules() -> str:
    if OUT_FILE.exists():
        m = re.search(re.escape(RULES_BEGIN) + r"\n(.*?)\n" + re.escape(RULES_END),
                      OUT_FILE.read_text(encoding="utf-8"), re.DOTALL)
        if m:
            return m.group(1).strip()
    return DEFAULT_RULES


def main():
    goals = GOALS_FILE.read_text(encoding="utf-8")
    doc = f"""---
generated: {datetime.now().isoformat(timespec='minutes')}
generator: generate_operating_context.py
retrieval_tier: primary
---

# Operating context (agent entry point)

Primary orientation file. Regenerated nightly — do not edit outside the hard-rules block.

## Active now (from personal/_goals.md)

{active_now_section(goals)}

## Memory layers

{memory_pointers()}
- `agent/lesson-queue.md` — pending procedural-memory proposals (check `## Pending`)
- `agent/_goals-agent.md` — agent working goals (nightly-maintained)

## Hard rules

{RULES_BEGIN}
{existing_rules()}
{RULES_END}
"""
    AGENT_DIR.mkdir(exist_ok=True)
    OUT_FILE.write_text(doc, encoding="utf-8")
    size = OUT_FILE.stat().st_size
    print(f"operating-context: written {size} B{' ⚠️ over 3 KB target' if size > 3072 else ''}")


if __name__ == "__main__":
    main()
