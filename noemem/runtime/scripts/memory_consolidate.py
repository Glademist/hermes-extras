#!/usr/bin/env python3
"""memory_consolidate.py — Phase 2 organize step (dreaming: Organize).

Takes yesterday's (or --date) valid archives, routes each to its memory/ file
(same DOMAIN_MAP as memory_seed), extracts durable facts and rewrites only the
affected memory files (LLM gets current file + new facts). Git commit is done
by the nightly wrapper afterwards.

Slow-model budget: 1 extract call (all archives of the day batched) +
1 write call per touched memory file. Typical night = 2-3 calls.

Usage:
  python3 memory_consolidate.py                # yesterday's archives
  python3 memory_consolidate.py --date 2026-07-09
  python3 memory_consolidate.py --dry-run      # show routing, no LLM
"""
import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR.parent))
sys.path.insert(0, str(_SCRIPTS_DIR))
from noemem_paths import (  # noqa: E402
    ARCHIVE_DIR, CHARS_PER_WORD, FILE_CHAR_CAP, FILE_CHAR_CAP_WORDS, FILE_CHAR_TARGET,
    FILE_CHAR_TARGET_WORDS, NOEMEM_ROOT,
)
from archive_lint import lint_file, load_catalog_slugs, parse_frontmatter  # noqa: E402
from memory_seed import (  # noqa: E402
    DOMAIN_MAP,
    FILE_DESCRIPTIONS,
    EXTRACT_PROMPT,
    parse_extract,
    parse_verdicts,
    record_text,
    target_file,
)
import dreamer_v2  # noqa: E402
from dreamer_v2 import call_llm  # noqa: E402

MEMORY_DIR = NOEMEM_ROOT / "memory"
# The merge rewrites a WHOLE file, so max_tokens must cover the largest memory
# file (~3 chars/token) PLUS any reasoning the backend spends anyway. History of
# this constant: 3500 truncated projects.md mid-sentence (2026-10-04); 8192 still
# was not enough, because a run burned all 8192 completion tokens on reasoning.
# 2026-10-08 the real cause was found: the Nous endpoint IGNORES `enable_thinking`,
# so these calls ran with thinking ON (default_effort=high) and reasoning ate the
# budget. nous_client.prepare_chat_body now sends reasoning_effort="none" (measured:
# reasoning=0, a full merge uses ~2800 tokens). 32768 is generous insurance on top:
# the largest file (~9500 chars ≈ 3300 tokens) leaves >25k of slack, and the value
# only costs money if it is actually used.
MERGE_MAX_TOKENS = 32768
# How much of the current file is echoed into the merge prompt. Must stay >= the
# hard cap, or a file sitting just under the cap gets its own tail cut off before
# the model ever sees it (it was 14000 against a 15000 cap).
MEMORY_FILE_CHAR_CAP = FILE_CHAR_CAP

MERGE_PROMPT = """You maintain `memory/{fname}` — a durable memory file for an AI agent.

Scope: {description}

Below is the CURRENT file and NEW facts from yesterday's sessions ({day}).
Rewrite the file to integrate the new facts.

Rules:
- Keep the existing structure (`## sections`); add facts to the matching section or create one.
- ALWAYS keep near the top (after the `# Title`):
  1) a line `**Aliases:** ...` (merge new names into it, do not drop old aliases)
  2) a `## Stav teď` section with 5–15 bullets of *current* status only (paths, what works, what is open). Update Stav teď when new facts change status; do not bury current status only in deep sections.
- If a new fact contradicts or updates an old bullet, REPLACE the old bullet (append " (updated {day})" to the rewritten bullet). Do not leave two competing bullets.
- Never create a second parallel "current status" file idea — this memory file IS the current truth for the topic. `projekty/*-current.md` and archive are not replacements.
- Never invent facts not present below. Never delete unrelated *current* content; do not drop what is still true.
- **This file is CURRENT STATE ONLY. History is NOT allowed here.** No past events, no cleaned-up/deleted files, no old status blocks, no "Stav na disku (2026-09-03)"-style logs. Those belong in `archive/` (or `models-registry.md` for model history). If an old bullet is no longer true, rewrite it in place or delete it.
- **Never write your own reasoning, plan or commentary.** Forbidden: "Let's", "We need", "I think", "Need decide", "=== CURRENT FILE ===", "New facts", meta-text about how to structure the file. Output facts as bullets only — the file must read as a status document, not as a working note.
- Every *state* claim carries a date in parentheses ("ověřeno 2026-09-17", "od 2026-09-14"); a line with no date reads as history.
- **Size: aim for about {target_words} words (currently {cur_words}).** This is a soft goal, NOT a hard limit — going over it is fine and will not be rejected, so never drop a true fact just to hit a number. Do not try to count characters: you cannot do it accurately, and these files are mixed Czech/English so characters-per-word is not even constant. Judge by reading it.
- **The one size rule that IS local and checkable: every bullet under ~200 characters** (roughly 25 words). A bullet is a fact, not a paragraph — if it needs two lines, it is two bullets or it belongs in a skill. Long bullets are the single biggest reason these files grew: `secretary.md` averages 468 chars per bullet, `models-registry.md` 522. Detailed history belongs in `archive/` or `models-registry.md`, not here.
- Drop ephemeral junk when rewriting a bullet: one-off job_ids, temporary card ids, dead ports unless still true.
- Keep exact paths/commands/ports in `backticks`. Terse bullets. **Write in the language the file is already written in, and do not translate it.** This instruction used to say "English", which was unsatisfiable on the 12 memory files that are partly or wholly Czech: the model silently ignored it and kept the Czech, so the line was noise. the user is Czech-speaking and these files are his to read; a memory file is not a place to convert someone's notes behind their back.
- Output the COMPLETE updated file, starting with optional `<!-- last consolidated ... -->` then `# ` heading. No commentary.

=== CURRENT FILE ===
{current}

=== NEW FACTS ({day}) ===
{facts}"""


_REASONING_MARKERS = (
    "let's ", "let us ", "we need", "we can ", "we should", "we must",
    "i think", "as given", "need decide", "need to decide", "should we",
    "new facts", "=== current file", "the instruction says", "maybe we",
    "\nhmm", "hmm.",
)


def merge_output_ok(out: str) -> tuple[bool, str]:
    """Reject merge output that is reasoning/planning text or lost the file skeleton.

    The naive length check (>=50% of the previous file) once let a planning dump
    overwrite `memory/infra.md` (2026-09-27): long enough to pass, but the model
    had written its own deliberation instead of facts.
    """
    low = out.lower()
    for marker in _REASONING_MARKERS:
        if marker in low:
            return False, f"reasoning/planning text ({marker!r})"
    lines = out.splitlines()
    if not any(l.startswith("# ") for l in lines):
        return False, "missing '# Title' heading"
    if not any(l.startswith("## ") for l in lines):
        return False, "no '## ' section"
    return True, ""


# Budget: a topic file may not GROW beyond the cap (FILE_CHAR_CAP, shared with
# noemem_selfcheck via noemem_paths). A file already above the cap can still be
# rewritten (that is the compression path), but never enlarged — that is what
# turned `hermes-system.md` into 24 KB of piled-up history.
BULLET_WARN_CHARS = 300
# Lower clamp for the merge-output truncation floor (see use in main()). A real
# memory file is thousands of chars; a truncated LLM response is hundreds. Kept
# well under FILE_CHAR_CAP so compressing an over-budget file can still pass.
MIN_MERGE_FLOOR = 2000

# Appended to the merge prompt on the retry attempt when the first output was
# rejected for exceeding the cap. Vague "compress" instructions produced a file
# 200 chars over the limit every single night; the retry must state the shortfall
# and the concrete ceiling so the model has a target it can actually hit.
SHRINK_ORDER = """

=== HARD SIZE REQUIREMENT (your last attempt was {over} chars and will be REJECTED) ===
The rewritten file MUST be at most {cap} characters. That is a hard limit, not a
target — anything longer is discarded and the day's facts are lost. You are over
the limit, so you MUST cut. Cut in this order until you fit:
1. Whole `## ` sections that are history, changelogs or superseded status.
2. Bullets that restate another bullet, or duplicate a dedicated memory file
   (VAD/voice detail → voice.md, models → models.md, ports → infra.md).
3. Long bullets — rewrite every one under ~200 chars (~25 words). This is the
   step that does most of the work, and it needs no counting: just look at each
   bullet and cut any that runs past two lines.
4. The least useful remaining detail.
Keep the `# ` heading, the `**Aliases:**` line, `## Stav teď` and every fact that
is still true. Output the COMPLETE file only, no commentary."""


def budget_ok(new_text: str, current_text: str = "") -> tuple[bool, str]:
    """Two-tier budget: a soft target we only warn about, and a hard cap we enforce.

    The target exists to steer the model toward terse files; it must NEVER reject
    a write, because the model cannot count characters and a hard target just
    freezes the file (projects.md, 8 days, 2026-09-27 → 10-04). Detail reduction
    is the weekly memory_polish.py job. Only the cap — 2.5x the target, i.e.
    well above any legitimately detailed topic file — actually refuses a write.
    """
    cur = len(current_text)
    cap = FILE_CHAR_CAP if cur <= FILE_CHAR_CAP else cur
    if len(new_text) > cap:
        return False, f"over hard cap: {len(new_text)} chars > {cap} (split the topic)"
    long_bullets = [l for l in new_text.splitlines() if l.startswith("- ") and len(l) > BULLET_WARN_CHARS]
    if long_bullets:
        print(f"  ⚠️  budget: {len(long_bullets)} bullet(s) over {BULLET_WARN_CHARS} chars (compress)")
    return True, ""


def effective_cap(cur: int) -> int:
    """The ceiling `budget_ok` will actually enforce for a file of `cur` chars.

    The merge prompt used to be told FILE_CHAR_CAP (6000) while the gate enforced
    `cur` for an over-cap file (11373 for projects.md) — a contradiction the model
    could not resolve, so it re-emitted the file verbatim +new facts and was
    rejected by ~200 chars, every night, forever. The prompt must state the number
    the gate will use.
    """
    return FILE_CHAR_CAP if cur <= FILE_CHAR_CAP else cur


FAILURE_LOG = NOEMEM_ROOT / "runtime" / "state" / "memory_write_failures.jsonl"


def log_write_failure(fname: str, reason: str) -> None:
    """Persist a refused write so the daily selfcheck can report it, not just the log line."""
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"ts": datetime.now().isoformat(timespec="seconds"),
                 "file": fname, "reason": reason},
                ensure_ascii=False,
            ) + "\n")
    except OSError:
        pass


def _verdict_key(line: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()[:60]


def add_verdicts(lines: list[str], day: str) -> None:
    """Append durable trial-and-verdict lines to memory/verdicts.md — deduped, budget-capped."""
    vpath = MEMORY_DIR / "verdicts.md"
    if not vpath.exists():
        print("  ⚠️  verdicts.md missing — verdicts dropped")
        return
    current = vpath.read_text(encoding="utf-8")
    known = {_verdict_key(l.lstrip("- ")) for l in current.splitlines() if l.startswith("- ")}
    new = []
    for line in lines:
        k = _verdict_key(line)
        if k and k not in known:
            known.add(k)
            new.append(f"- {line} ({day})")
    if not new:
        print(f"  verdicts: {len(lines)} extracted, none new")
        return
    body = current.rstrip() + "\n" + "\n".join(new) + "\n"
    ok, why = budget_ok(body, current)
    if not ok:
        print(f"  ⚠️  verdicts.md: {why} — {len(new)} verdict(s) NOT added")
        log_write_failure("verdicts.md", why)
        return
    vpath.write_text(body, encoding="utf-8")
    print(f"  ✅ verdicts.md (+{len(new)})")


def select_day_archives(day: str) -> list[Path]:
    catalog = load_catalog_slugs()
    out = []
    for path in sorted(ARCHIVE_DIR.glob(f"{day}_*.md")):
        fm = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace")) or {}
        if fm.get("retrieval_tier") in ("superseded", "quarantine"):
            continue
        if lint_file(path, catalog)["errors"]:
            print(f"  ⏭️  {path.name}: lint errors, skipped (should be quarantined)")
            continue
        out.append(path)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=(date.today() - timedelta(days=1)).isoformat())
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    archives = select_day_archives(args.date)
    if not archives:
        print(f"consolidate: no valid archives for {args.date}, nothing to do")
        return

    catalog = load_catalog_slugs()
    routing: dict[str, list[Path]] = {}
    for p in archives:
        fm = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace")) or {}
        routing.setdefault(target_file(fm, catalog), []).append(p)

    print(f"consolidate {args.date}: {len(archives)} archives → {sorted(routing)}")
    if args.dry_run:
        for fname, paths in routing.items():
            for p in paths:
                print(f"  {fname} ← {p.name}")
        return

    # Pass 1: one extract call for the whole day, grouped output reused per file
    facts_by_file: dict[str, list[str]] = {}
    verdicts_all: list[str] = []
    extract_failures: list[str] = []
    for fname, paths in routing.items():
        records = "\n\n".join(record_text(p) for p in paths)
        out = call_llm(
            EXTRACT_PROMPT.format(n=len(paths), records=records),
            system="You are a precise fact extractor. Output only the requested format, in English.",
            max_tokens=1800,
            enable_thinking=False,
        )
        if not out:
            # Log BEFORE moving on. The old `sys.exit(1)` here ran before any
            # log_write_failure, so this entire failure class was invisible to the daily
            # selfcheck (reporting blind spot), AND it killed the whole day: one file's
            # extract failure also dropped every other file's facts. Skip just this file.
            print(f"  ❌ extract failed for {fname}; skipping (other files continue, retry next run)")
            log_write_failure(fname, "extract failed (empty response)")
            extract_failures.append(fname)
            continue
        facts, convs = parse_extract(out)
        facts_by_file.setdefault(fname, []).extend(facts)
        if convs:
            facts_by_file.setdefault("conventions.md", []).extend(convs)
        vd = parse_verdicts(out)
        verdicts_all.extend(vd)
        print(f"  {fname}: {len(facts)} facts, {len(convs)} conventions, {len(vd)} verdicts")

    # Pass 2: merge per memory file
    MEMORY_DIR.mkdir(exist_ok=True)
    for fname, facts in facts_by_file.items():
        if not facts:
            continue
        fpath = MEMORY_DIR / fname
        current = fpath.read_text(encoding="utf-8") if fpath.exists() else f"# {fname.removesuffix('.md').title()}\n"
        # strip seed header comment from LLM input; re-add nothing (git tracks provenance)
        current_body = "\n".join(l for l in current.splitlines() if not l.startswith("<!--")).strip()
        cur_chars = len(current_body)
        cap_chars = effective_cap(cur_chars)
        # Measured 2026-10-04: with "aim for ~6000" the model still rewrote a
        # 6862-char file down to 3297 and dropped 6 of 18 bullets — it reads a
        # target as pressure even when the file already complies. So when the
        # file is already near the target, say explicitly that shrinking is wrong.
        if cur_chars <= FILE_CHAR_TARGET * 1.25:
            size_note = (f"The file is currently {cur_chars} chars, i.e. already at or below the "
                         f"{FILE_CHAR_TARGET} target. **Do NOT shorten it.** Keep every existing "
                         f"bullet that is still true and simply add the new facts.")
        else:
            size_note = ""
        merge_prompt = MERGE_PROMPT.format(
            fname=fname,
            description=FILE_DESCRIPTIONS.get(fname, ""),
            day=args.date,
            current=current_body[:MEMORY_FILE_CHAR_CAP],
            facts="\n".join(f"- {f}" for f in facts),
            target_words=FILE_CHAR_TARGET_WORDS,
            cur_words=round(cur_chars / CHARS_PER_WORD),
            target=FILE_CHAR_TARGET,
            cap=cap_chars,
            cur=cur_chars,
        ) + size_note
        retry_prompt = merge_prompt
        out = None
        for attempt in (1, 2):
            out = call_llm(
                retry_prompt,
                system="You are a technical writer maintaining agent memory files. Output the complete file in the same language the existing content is written in. Output the full file.",
                max_tokens=MERGE_MAX_TOKENS,
                enable_thinking=False,
            )
            # Truncation guard: reject a fragment, not a compressed file. The floor is
            # 50 % of the current file, CLAMPED to MIN_MERGE_FLOOR — the merge prompt
            # orders the model to compress a file that is over budget, so a compliant
            # answer for `teacher.md` (25065 chars) is ~5000 chars, which the
            # unclamped 50 % floor (12532) rejected as "suspiciously short". The guard
            # therefore punished exactly the compression the budget rule requires and
            # froze the five largest files permanently (2026-10-01). A floor tied to
            # FILE_CHAR_CAP itself would be just as wrong in the other direction: it
            # demands output >= cap while budget_ok() demands output <= cap, so any
            # well-compressed file below the cap is still frozen. The floor's only job
            # is to catch a truncated response, and a fragment is orders of magnitude
            # smaller than a real file.
            #
            # The floor is applied ONLY to a file that is actually over cap: below the
            # cap the model is under no obligation to compress, so a short-but-complete
            # rewrite is legitimate output (conventions.md 5732 → 1237 was rejected for
            # this on 2026-10-04, the mirror image of the 10-01 bug).
            floor = min(cur_chars * 0.5, MIN_MERGE_FLOOR) if cur_chars > FILE_CHAR_CAP else 0
            if not out:
                if attempt == 2:
                    why = "empty response"
                    print(f"  ⚠️  {fname}: {why} — file left unchanged")
                    log_write_failure(fname, why)
                continue
            if len(out.strip()) < floor:
                if attempt == 2:
                    why = f"merge output too short: {len(out.strip())} < {floor:.0f} chars"
                    print(f"  ⚠️  {fname}: {why} — file left unchanged")
                    log_write_failure(fname, why)
                continue
            ok, why = merge_output_ok(out)
            if not ok:
                # Retryable like the two cases above: a missing heading or a lost alias
                # line is a formatting slip in a response we did not otherwise reject,
                # and infra.md lost a whole night of facts to one at 03:16 on 2026-10-05
                # because this was the ONLY rejection class that gave up immediately.
                if attempt == 2:
                    print(f"  ⚠️  {fname}: merge output REJECTED ({why}) — file left unchanged")
                    log_write_failure(fname, why)
                    # Clear it: a rejected output must never fall through to the write
                    # below, or the gate would be enforced only in the log.
                    out = None
                else:
                    print(f"  ⚠️  {fname}: {why} — retrying once with the same prompt")
                continue
            # A response cut mid-sentence (finish_reason=length) is non-empty and
            # passes every other gate, but writing it destroys the tail of the file.
            # This is how projects.md lost its models-registry section on 2026-10-04.
            # RETRYABLE like the empty / too-short / malformed classes: a second sample
            # usually comes back complete (call_llm already escalates the budget on a
            # "length" reply). Giving up on the first attempt is what silently dropped
            # a night of projects.md facts on 2026-10-07 13:49 and 10-08 03:11.
            if dreamer_v2._LAST_FINISH_REASON == "length":
                why = "merge output truncated by max_tokens (finish_reason=length)"
                if attempt == 2:
                    print(f"  ⚠️  {fname}: {why} — file left unchanged")
                    log_write_failure(fname, why)
                    out = None
                else:
                    print(f"  ⚠️  {fname}: {why} — retrying once")
                continue
            ok, why = budget_ok(out, current_body)
            if ok:
                if len(out) > FILE_CHAR_TARGET:
                    # Soft target exceeded: allowed. Print only — writing this to
                    # memory_write_failures.jsonl made a SUCCESSFUL write look like
                    # a refused one, and the selfcheck alerted on it at 06:20 (2026-10-05).
                    # Detail reduction is memory_polish.py's job, not the incident log's.
                    print(f"  ℹ️  {fname}: {len(out)} chars > soft target {FILE_CHAR_TARGET} "
                          f"— allowed, weekly polish will tighten it")
                break
            # Over the HARD cap: one escalating retry with a concrete target. The
            # cap is 2.5x the target, so this means the file genuinely outgrew
            # its topic — retry once, then refuse rather than silently truncate.
            print(f"  ⚠️  {fname}: {why}")
            if attempt == 2:
                # Clear it: a rejected output must never fall through to the write
                # below, or the gate would be enforced only in the log.
                print(f"  ⚠️  {fname}: file left unchanged")
                log_write_failure(fname, why)
                out = None
            else:
                print(f"  ⚠️  {fname}: retrying with an explicit shrink target")
                # Drop the "do NOT shorten it" note first, or it directly
                # contradicts the shrink order appended below.
                retry_prompt = (merge_prompt.replace(size_note, "") + "\n"
                                + SHRINK_ORDER.format(over=len(out), cap=cap_chars))
        if not out:
            continue
        # The model sometimes echoes the existing stamp back as part of its output. We
        # strip it from the body we send AND from what we write, otherwise every run
        # stacks another one (infra.md had two by 2026-10-05).
        clean = "\n".join(l for l in out.strip().splitlines()
                          if not l.strip().startswith("<!-- last consolidated")).strip()
        stamp = f"<!-- last consolidated {datetime.now().isoformat(timespec='minutes')} from {args.date} archives -->\n\n"
        fpath.write_text(stamp + clean + "\n", encoding="utf-8")
        print(f"  ✅ {fname} updated ({len(facts)} new facts, {len(clean)} chars)")

    # Pass 3: durable verdicts (long-term register)
    if verdicts_all:
        add_verdicts(verdicts_all, args.date)

    print("consolidate done")
    if extract_failures:
        print(f"consolidate: extract failed for {', '.join(extract_failures)} — retried next run")
        sys.exit(1)


if __name__ == "__main__":
    main()
