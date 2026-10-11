#!/usr/bin/env python3
"""noemem_selfcheck.py — denní kontrola paměťové pipeline (nezávislá na LLM).

Proč: týdenní reporty odhalí problém za týden. Tenhle běh řekne ráno, co se
pokazilo v noci — a je tichý, když je vše OK (jedna zpráva jen při problému).

Co kontroluje:
  1. Odmítnuté zápisy do memory/ (memory_write_failures.jsonl, posledních 24 h)
     — rozpočet / reasoning text / krátký výstup.
  2. Sink telemetrie: stáří logs/qmd-retrieval-<YYYY-MM>.jsonl (> 26 h = mrtvý).
  3. Prefetch žije: ≥ 1 prefetch event za 24 h (jinak se provider nepřipojil).
  4. Soubory memory/*.md nad rozpočtem — mohou jen klesat (riziko zamrznutí).

Hlídač odmítnutých zápisů NETLAČÍ exit 1 na známý stav: odmítnutí je často
gate ÚSPĚŠNĚ odmítl růst (viz memory_consolidate.budget_ok). Nové/změněné
odmítnutí = problém (exit 1); totožné opakované = poznámka. Bez toho selfcheck
bučel každé ráno na stav, který trvá 7 dní, a člověk musel přijít bez chyby.

Výstup: prázdný při úspěchu (exit 0), jinak ❌ blok (exit 1).
Vždy se zapíše logs/selfcheck-YYYY-MM-DD.log, i když je ticho.
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

ROOT = Path.home() / "hermes-noemem"
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "runtime" / "scripts"))
from noemem_paths import FILE_CHAR_CAP, FILE_CHAR_TARGET  # noqa: E402

LOGS = ROOT / "logs"
STATE = ROOT / "runtime" / "state"
FAILURES = STATE / "memory_write_failures.jsonl"
SEEN_STATE = STATE / "selfcheck_reported.json"
SINK_MAX_AGE_H = 26

now = datetime.datetime.now(datetime.timezone.utc)
problems: list[str] = []
notes: list[str] = []


def parse_ts(text: str) -> datetime.datetime | None:
    try:
        ts = datetime.datetime.fromisoformat(text)
        return ts if ts.tzinfo else ts.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


# 1) refused writes in the last 24 h.
# A refusal is only an INCIDENT when it is new or has changed. An identical
# refusal that was already reported is a steady state — usually the budget gate
# correctly refusing to let an over-cap file grow — and belongs in notes, or the
# job cries wolf every morning until a human intervenes for something that is
# not actually broken.
recent: dict[str, int] = {}
if FAILURES.exists():
    for line in FAILURES.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = parse_ts(str(row.get("ts", "")))
        if ts and (now - ts) < datetime.timedelta(hours=24):
            key = f"{row.get('file')} — {row.get('reason')}"
            recent[key] = recent.get(key, 0) + 1

try:
    seen: dict = json.loads(SEEN_STATE.read_text(encoding="utf-8")) if SEEN_STATE.exists() else {}
    if not isinstance(seen, dict):
        seen = {}
except (OSError, json.JSONDecodeError):
    seen = {}

newly_reported: list[str] = []
repeat: list[str] = []
for key, n in sorted(recent.items(), key=lambda kv: -kv[1]):
    label = f"odmítnutý zápis memory/ ({n}×): {key[:150]}"
    if key in seen:
        repeat.append(label + " — známé, opakované (gate drží, ne nový incident)")
    else:
        problems.append(label)
        newly_reported.append(key)
for label in repeat:
    notes.append(label)

# Remember what we alerted on, so tomorrow the same refusal is a note. Keys that
# stopped occurring age out of the window on their own.
for k in list(seen):
    if k not in recent:
        del seen[k]
seen.update({k: now.astimezone().isoformat(timespec="seconds") for k in newly_reported})
try:
    SEEN_STATE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_STATE.write_text(json.dumps(seen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
except OSError as e:
    print(f"⚠️  selfcheck: nelze uložit stav hlídače ({e})")

# 2) telemetry sink alive
sink = LOGS / f"qmd-retrieval-{now.astimezone().strftime('%Y-%m')}.jsonl"
if not sink.exists():
    problems.append(f"telemetrie chybí: {sink.name} neexistuje")
else:
    age_h = (now - datetime.datetime.fromtimestamp(sink.stat().st_mtime, datetime.timezone.utc)).total_seconds() / 3600
    if age_h > SINK_MAX_AGE_H:
        problems.append(f"telemetrie mrtvá: {sink.name} stará {age_h:.1f} h (> {SINK_MAX_AGE_H} h)")

    # 3) prefetch alive + noise-filter stats
    pref = empty = filt = 0
    for line in sink.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = parse_ts(str(row.get("ts", "")))
        if not ts or (now - ts) >= datetime.timedelta(hours=24):
            continue
        if row.get("channel") != "prefetch":
            continue
        pref += 1
        if row.get("filtered"):
            filt += 1
        elif row.get("empty"):
            empty += 1
    if pref == 0:
        problems.append("prefetch za 24 h nespustil jediný event (provider se nepřipojil?)")
    else:
        notes.append(f"prefetch 24 h: {pref} eventů, {empty} prázdných, {filt} filtrovaných")

# 4) memory file sizes. Over the soft TARGET is advisory only — the weekly
# memory_polish.py pass is what reduces detail. Only the hard CAP is a problem.
over_target = []
over_cap = []
for p in sorted((ROOT / "memory").glob("*.md")):
    n = len(p.read_text(encoding="utf-8"))
    if n > FILE_CHAR_CAP:
        over_cap.append(f"{p.name} {n}")
    elif n > FILE_CHAR_TARGET:
        over_target.append(f"{p.name} {n}")
if over_cap:
    problems.append("nad tvrdým limitem (je potřeba rozdělit téma): " + ", ".join(over_cap))
if over_target:
    notes.append(f"nad měkkým cílem {FILE_CHAR_TARGET} (povoleno, týdenní polish zkrátí): "
                 + ", ".join(over_target))

log_line = f"{now.astimezone().isoformat(timespec='seconds')} " + ("OK" if not problems else "FAIL")
(LOGS / f"selfcheck-{now.astimezone().strftime('%Y-%m-%d')}.log").write_text(
    log_line + "\n" + "\n".join(f"- {p}" for p in problems) + "\n" + "\n".join(f"  {n}" for n in notes) + "\n",
    encoding="utf-8",
)

if problems:
    print(f"🔴 noemem selfcheck — {now.astimezone().strftime('%Y-%m-%d %H:%M')}")
    for p in problems:
        print(f"- {p}")
    for n in notes:
        print(f"  ({n})")
    sys.exit(1)
sys.exit(0)