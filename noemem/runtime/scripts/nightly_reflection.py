#!/usr/bin/env python3
"""Nightly Reflection — goal-driven session analysis.

Modes:
  --daily    (default): včerejší Dreamer archivy → přehled do _goals-agent.md
  --weekly:             7d archivy + rotace *_current.md → _goals-agent.md

_goals.md se nemění (uživatel). Starší _goals-agent → agent/goals-agent-snapshots/.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
_RUNTIME_DIR = _SCRIPTS_DIR.parent
sys.path.insert(0, str(_RUNTIME_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR))
from noemem_paths import (  # noqa: E402
    ARCHIVE_DIR,
    GOALS_AGENT_FILE,
    GOALS_AGENT_SNAPSHOTS,
    GOALS_FILE,
    NOEMEM_ROOT,
    PERSONAL_DIR,
    PROFILE_FILE,
    PROJEKTY_CURRENT_DIR,
    SCRIPTS_DIR,
)
from api_lock import with_api_lock  # noqa: E402
from mtplx_client import parse_assistant_content, post_chat, prepare_chat_body  # noqa: E402
from multipass_nightly import run_multipass_nightly  # noqa: E402
from projekt_utils import format_catalog_for_prompt, load_project_catalog  # noqa: E402

MEMORY_DIR = NOEMEM_ROOT
PROJEKTY_DIR = PROJEKTY_CURRENT_DIR
_ROTATE_SCRIPTS = SCRIPTS_DIR

# ===================== UTILITY =====================


def parse_frontmatter(text: str) -> dict:
    meta = {}
    # search, not match: superseded/quarantined archives carry a "> **[...]**"
    # banner before the frontmatter block
    m = re.search(r'^---\s*\n(.*?)\n---', text, re.DOTALL | re.MULTILINE)
    if m:
        for line in m.group(1).split('\n'):
            if ':' in line:
                key, _, val = line.partition(':')
                meta[key.strip()] = val.strip()
    return meta


def extract_date_from_filename(name: str) -> Optional[datetime]:
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", name)
    if m:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    return None


def parse_dreamer_fields(text: str) -> dict:
    """Parse Dreamer archive body (TÉMA, VÝSLEDEK, …)."""
    out = {}
    for key in ("TÉMA", "PROČ", "VÝSLEDEK", "PONAUČENÍ", "PROJEKT"):
        m = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
        if m:
            out[key] = m.group(1).strip()[:600]
    return out


def get_files_since(
    hours: int,
    min_importance: int = 1,
    mode: str = "daily",
) -> list[dict]:
    """Select sessions by YYYY-MM-DD in filename only (no mtime fallback)."""
    today = datetime.now(timezone.utc).date()
    if mode == "daily":
        target_dates = {today - timedelta(days=1)}
    elif mode == "weekly":
        target_dates = {today - timedelta(days=i) for i in range(1, 8)}
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        target_dates = None

    files = []
    search_roots = [ARCHIVE_DIR] if ARCHIVE_DIR.is_dir() else []
    if not search_roots:
        search_roots = [MEMORY_DIR]

    for root in search_roots:
        for path in sorted(root.rglob("*.md")):
            if path.name.startswith("_"):
                continue
            file_date = extract_date_from_filename(path.name)
            if file_date is None:
                continue
            if target_dates is not None and file_date not in target_dates:
                continue
            if target_dates is None:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    continue
            text = path.read_text(encoding="utf-8")
            meta = parse_frontmatter(text)
            if meta.get("retrieval_tier") in ("superseded", "quarantine"):
                continue
            dreamer = parse_dreamer_fields(text)
            title = dreamer.get("TÉMA") or path.stem
            summary = dreamer.get("VÝSLEDEK", "")[:400]
            bullets = []
            if dreamer.get("PROČ"):
                bullets.append(dreamer["PROČ"][:200])
            if dreamer.get("PONAUČENÍ"):
                bullets.append(dreamer["PONAUČENÍ"][:200])
            if not summary:
                summary_match = re.search(r"## Shrnutí\s*\n(.+?)(?:\n##|\Z)", text, re.DOTALL)
                summary = summary_match.group(1).strip()[:300] if summary_match else ""
            importance = int(meta.get("importance", 5) or 5)
            if importance < min_importance:
                continue
            project = meta.get("project") or dreamer.get("PROJEKT", "")
            sid = (meta.get("source_session_id") or "").strip()
            files.append({
                "title": title,
                "summary": summary,
                "bullets": bullets[:4],
                "importance": importance,
                "category": meta.get("category", "unknown"),
                "relates_to": meta.get("relates_to", ""),
                "path": str(path.relative_to(MEMORY_DIR)),
                "file_date": file_date,
                "project": project,
                "archive_name": path.name,
                "source_session_id": sid,
            })
    files.sort(key=lambda f: f.get("file_date", datetime.min.date()), reverse=True)
    return files


def rotate_all_project_current() -> None:
    """Weekly: archive <key>_current.md before analysis."""
    if not PROJEKTY_DIR.is_dir():
        print("ℹ️  projekty/ neexistuje, rotace přeskočena")
        return
    sys.path.insert(0, str(_ROTATE_SCRIPTS))
    try:
        from rotate_current import rotate_current
    except ImportError as e:
        print(f"⚠️  rotate_current import failed: {e}")
        return
    rotated = 0
    for path in sorted(PROJEKTY_DIR.glob("*-current.md")):
        if path.name.startswith("_"):
            continue
        key = path.name[: -len("-current.md")]
        if not key:
            continue
        rotate_current(key, PROJEKTY_DIR)
        print(f"   📦 {key}-current.md → weekly/ + nový")
        rotated += 1
    if rotated == 0:
        print("   ℹ️  Žádné *-current.md k rotaci")
    else:
        print(f"   ✅ Rotováno {rotated} souborů")


def archive_run_stats(files: list[dict]) -> tuple[int, int, list[str]]:
    """Počet live archivních souborů a unikátních Hermes session_id."""
    n_files = len(files)
    sids: list[str] = []
    seen: set[str] = set()
    for f in files:
        sid = (f.get("source_session_id") or "").strip()
        if sid and sid not in seen:
            seen.add(sid)
            sids.append(sid)
    n_unique = len(sids) if sids else n_files
    return n_files, n_unique, sids


def write_nightly_log(mode: str, files: list[dict], note: str = "") -> None:
    from datetime import date

    n_files, n_unique, sids = archive_run_stats(files)
    log_dir = MEMORY_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"nightly-{mode}-{date.today().isoformat()}.log"
    with open(log_file, "w", encoding="utf-8") as lf:
        lf.write(f"Nightly {mode} — {date.today().isoformat()}\n")
        lf.write("status: ok\n")
        lf.write(f"archive_files: {n_files}\n")
        lf.write(f"unique_sessions: {n_unique}\n")
        if note:
            lf.write(f"Note: {note}\n")
        lf.write("\n")
        for f_item in files:
            sid = f_item.get("source_session_id") or "?"
            lf.write(f"- {f_item['path']} (session_id={sid})\n")
        if sids:
            lf.write("\n# Unikátní session_id:\n")
            for sid in sids:
                lf.write(f"- {sid}\n")
    print(f"📝 Log: {log_file.name} ({n_files} archivů, {n_unique} konverzací)")


NIGHTLY_MAX_TOKENS = 12288
NIGHTLY_THINKING_MIN_TOKENS = 8192

# Role config from runtime/model_config.json (nightly_extract / nightly_write).
from noemem_paths import role_config  # noqa: E402
_NIGHTLY_EXT = role_config("nightly_extract") or {}
_NIGHTLY_WRITE = role_config("nightly_write") or {}
if _NIGHTLY_EXT:
    NIGHTLY_MAX_TOKENS = int(_NIGHTLY_EXT.get("max_tokens", NIGHTLY_MAX_TOKENS))
    NIGHTLY_THINKING_MIN_TOKENS = int(_NIGHTLY_EXT.get("max_tokens", NIGHTLY_THINKING_MIN_TOKENS))


@with_api_lock
def call_llm_messages(
    messages: list[dict],
    max_tokens: int = NIGHTLY_MAX_TOKENS,
    *,
    enable_thinking: bool = True,
) -> Optional[str]:
    """Volání lokálního LLM s thinking (bench: 100 % parse na daily i weekly)."""
    token_budget = (
        max(max_tokens, NIGHTLY_THINKING_MIN_TOKENS)
        if enable_thinking
        else max_tokens
    )
    try:
        body = prepare_chat_body(
            {
                "messages": messages,
                "temperature": 0.15,
                "max_tokens": token_budget,
                "enable_thinking": enable_thinking,
            }
        )
        resp = post_chat(body, timeout=1200)
        resp.raise_for_status()
        text = parse_assistant_content(resp.json()["choices"][0]["message"])
        return text if text else None
    except Exception as e:
        print(f"❌ LLM error: {e}", file=sys.stderr)
        return None


def _nightly_chat_adapter(_model, messages, params, **kwargs):
    del _model, kwargs
    text = call_llm_messages(
        messages,
        max_tokens=int(params.get("max_tokens", NIGHTLY_MAX_TOKENS)),
    )
    return {"content": text or ""}


# ===================== CORE LOGIC =====================


def build_session_summary(files: list[dict], max_chars: int = 8000) -> str:
    """Build compact input from memory files, respecting token budget."""
    lines = ["## Dreamer archivy (včerejší / týdenní výběr):"]
    total = 0
    for f in files:
        an = f.get("archive_name") or f["path"]
        proj = f.get("project") or "?"
        entry = f"\n[{f['importance']}/10 | {f['category']} | {proj}] {an}"
        entry += f"\n  TÉMA: {f['title']}"
        if f["summary"]:
            entry += f"\n  VÝSLEDEK: {f['summary']}"
        if f['bullets']:
            entry += "\n  - " + "\n  - ".join(f['bullets'])
        if total + len(entry) > max_chars:
            lines.append(f"\n... a {len(files) - len(lines) + 1} dalších session (oříznuto)")
            break
        lines.append(entry)
        total += len(entry)
    return "\n".join(lines)


def load_goal_tree() -> str:
    """Load current goal tree, strip frontmatter."""
    if not GOALS_FILE.exists():
        return "Žádný goal strom nenalezen."
    text = GOALS_FILE.read_text(encoding="utf-8")
    # Strip frontmatter
    text = re.sub(r'^---\s*\n.*?\n---\s*\n', '', text, flags=re.DOTALL)
    return text.strip()


def run_reflection(files: list[dict], *, mode: str = "daily") -> Optional[str]:
    """Multipass nightly: extract → write → verify retry."""
    max_chars = 10_000 if mode == "weekly" else 8000
    sessions = build_session_summary(files, max_chars=max_chars)
    goals = load_goal_tree()
    catalog = format_catalog_for_prompt(load_project_catalog())
    result = run_multipass_nightly(
        goals,
        sessions,
        _nightly_chat_adapter,
        "",
        mode=mode,
        catalog=catalog,
    )
    if result.get("verify_retry"):
        miss = result.get("missing_sections_after") or []
        print(f"  ↻ nightly verify retry (missing: {', '.join(miss[:3])})")
    return result.get("content")


def snapshot_goals_agent_before_write() -> Optional[str]:
    """Archive previous _goals-agent.md; return snapshot filename for supersedes."""
    if not GOALS_AGENT_FILE.exists():
        return None
    GOALS_AGENT_SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d")
    snap_name = f"goals-agent_{date}.md"
    snap_path = GOALS_AGENT_SNAPSHOTS / snap_name
    if not snap_path.exists():
        snap_path.write_text(GOALS_AGENT_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    return snap_name


def update_goals_agent(analysis: str) -> None:
    """Replace _goals-agent.md with superseding overview (not a diary in _goals.md)."""
    supersede = snapshot_goals_agent_before_write()
    body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", analysis, count=1, flags=re.DOTALL).strip()
    sup_line = f"supersedes: agent/goals-agent-snapshots/{supersede}\n" if supersede else ""
    header = f"""---
category: agent
importance: 8
keywords: goals-agent, agent-plan, reflection
{sup_line}updated: {datetime.now().isoformat(timespec="seconds")}
---

# Goals — Agent's View

*(Operativní přehled agenta: mapování archivů na projekty, stav podle _goals.md uživatele. Každý nightly run nahrazuje tento soubor.)*

"""
    GOALS_AGENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    GOALS_AGENT_FILE.write_text(header + body + "\n", encoding="utf-8")
    print("✅ _goals-agent.md aktualizován (superseding přehled)")


def update_profile(analysis: str):
    """Update _vztahovy-profil.md with relationship-relevant findings."""
    # Look for "Vztahové patterny" or "Návrhy do _vztahovy-profil.md"
    profile_section = None
    section_markers = [
        "### Vztahový profil",
        "Návrhy do _vztahovy-profil.md",
        "VZTAHOVÉ PATTERNY",
        "Vztahové patterny",
    ]
    lines = analysis.split("\n")
    for marker in section_markers:
        for i, line in enumerate(lines):
            if marker in line:
                profile_section = "\n".join(lines[i:i+10])
                break
        if profile_section:
            break

    if profile_section and PROFILE_FILE.exists():
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        profile_text = PROFILE_FILE.read_text(encoding="utf-8")
        append = f"""
## Nightly update ({now})
{profile_section}
"""
        profile_text += append
        PROFILE_FILE.write_text(profile_text, encoding="utf-8")
        print(f"✅ _vztahovy-profil.md aktualizován")
    elif profile_section:
        print("ℹ️  _vztahovy-profil.md neexistuje, přeskočeno")


# ===================== MAIN =====================


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nightly Reflection (multipass)")
    parser.add_argument("--mode", choices=["daily", "weekly"], default="daily",
                        help="daily = 24h (default), weekly = 7d")
    parser.add_argument("--hours", type=int, default=0,
                        help="Override lookback v hodinách")
    parser.add_argument("--min-importance", type=int, default=1,
                        help="Minimální importance session (def: 1 — vše relevantní)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Jen analyzovat, neukládat")
    args = parser.parse_args()

    if args.hours:
        lookback = args.hours
    elif args.mode == "weekly":
        lookback = 168  # 7 days
    else:
        lookback = 24

    print(f"🔍 Nightly Reflection — {args.mode} multipass (lookback: {lookback}h)")

    if args.mode == "weekly" and not args.dry_run:
        print("📦 Weekly: rotace *_current.md (před analýzou týdne)")
        rotate_all_project_current()

    files = get_files_since(lookback, min_importance=args.min_importance, mode=args.mode)
    n_files, n_unique, _ = archive_run_stats(files)
    print(f"📄 Nalezeno {n_files} archivních souborů ({n_unique} unikátních konverzací)")

    if not files:
        print("⚠️  Žádné Dreamer archivy v období (filtr: datum v názvu souboru)")
        write_nightly_log(args.mode, files, note="no sessions in period")
        return

    analysis = run_reflection(files, mode=args.mode)

    if not analysis:
        print("❌ Analýza selhala")
        return

    print(f"\n{'='*60}")
    print("ANALÝZA:")
    print(analysis)
    print(f"{'='*60}")

    if args.dry_run:
        print("ℹ️  Dry-run — neukládám")
        return

    update_goals_agent(analysis)
    update_profile(analysis)

    # Generate a compact notification message
    # Extract key changes for delivery
    lines = analysis.split("\n")
    highlights = [l for l in lines if l.strip().startswith(("-", "✅", "🔄", "⚠️", "🤔", "🆕"))]
    if highlights:
        print(f"\n📋 Klíčové body ({len(highlights)}):")
        for h in highlights[:8]:
            print(f"   {h.strip()}")

    print("🏁 Nightly Reflection dokončen")
    write_nightly_log(args.mode, files)


if __name__ == "__main__":
    main()