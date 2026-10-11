#!/usr/bin/env python3
"""Retrieval usefulness — deterministic proxy scoring for QMD retrieval events.

Reads the retrieval telemetry JSONL (~/hermes-noemem/logs/qmd-retrieval-YYYY-MM.jsonl)
and answers the user's question from 2026-09-22: "was the returned information actually
usable and beneficial?"

Honest scope (important):
- "Beneficial" cannot be measured directly. This script computes three deterministic
  USAGE signals per retrieval event and reports the ratio. It is a proxy, not proof.
- Signals (checked in the same session, AFTER the event's timestamp):
    reopened    — the agent later explicitly fetched a hit (mcp get/multi_get/read_resource)
    re_searched — a later explicit query in the same session names the hit file
    surfaced    — a later assistant message in the same session names the hit file
  A hit with NO signal = injected/found but never used again.
- Layer split (memory/archive/projekty/agent) tells us WHICH part of the tree is
  actually useful — that feeds write fixes, not blame.

Usage:
    retrieval_usefulness.py [--days 7] [--json]
Stdout: short human summary (or JSON with --json). Exit 0 always (fail-open),
prints "no events" when there is nothing to score.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG_DIR = Path.home() / "hermes-noemem" / "logs"
STATE_DB = Path.home() / ".hermes" / "state.db"

# Layer classification mirrors qmd_retrieval_log.py so both tools agree.
_LAYER_RULES = (
    ("memory", ("hermes-noemem-memory", "/memory/", "memory/")),
    ("current", ("hermes-noemem-projekty", "/projekty/", "-current.md")),
    ("archive", ("hermes-noemem-archive", "/archive/", "archive/")),
    ("agent", ("hermes-noemem-agent", "/agent/")),
    ("personal", ("hermes-noemem-personal", "/personal/")),
)


def classify_layer(path: str) -> str:
    p = (path or "").lower().replace("\\", "/")
    for name, needles in _LAYER_RULES:
        if any(n.lower() in p for n in needles):
            return name
    return "none" if not p else "other"


def _iso_to_epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def load_events(days: int) -> list[dict]:
    """All telemetry events from the last N days, across monthly files."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    events = []
    for f in sorted(LOG_DIR.glob("qmd-retrieval-*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("ts", "") >= cutoff and ev.get("n_hits", 0) > 0:
                events.append(ev)
    return events


def count_filtered(days: int) -> int:
    """Prefetch events skipped by the Wave 4 noise filter (system texts).

    These carry "filtered": "<reason>" and n_hits=0 — they are deliberate
    skips, not empty searches, so they are reported separately.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    n = 0
    for f in sorted(LOG_DIR.glob("qmd-retrieval-*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("ts", "") >= cutoff and ev.get("filtered"):
                n += 1
    return n


def load_session_messages(session_id: str) -> list[dict]:
    """(role, content, tool_name, timestamp) rows for one session, read-only."""
    try:
        # mode=ro: never touch the live DB from a reporting script
        conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT role, content, tool_name, timestamp FROM messages"
            " WHERE session_id = ? AND active = 1 ORDER BY timestamp, id",
            (session_id,),
        ).fetchall()
        # one dict per row, cheap and uniform for the matching below
        return [
            {"role": r[0] or "", "content": str(r[1] or ""), "tool_name": r[2] or "", "ts": r[3] or 0}
            for r in rows
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _stem(path: str) -> str:
    """'hermes-noemem-memory/secretary.md' → 'secretary'; short/generic stems skipped."""
    base = (path or "").rstrip("/").split("/")[-1]
    stem = re.sub(r"\.md$", "", base, flags=re.IGNORECASE)
    # generic stems match too much (index, _template, current); ignore them
    if len(stem) < 5 or stem.lower() in {"index", "_index", "readme", "current"}:
        return ""
    return stem


def score_event(ev: dict, messages: list[dict]) -> dict:
    """One retrieval event → which of its hits were actually used afterwards."""
    ev_epoch = _iso_to_epoch(ev.get("ts", ""))
    # only messages that exist AFTER the retrieval moment count as follow-up
    later = [m for m in messages if m["ts"] and m["ts"] > ev_epoch]

    used, seen_layers = [], set()
    for hit in ev.get("hits", []):
        path = str(hit.get("path") or "")
        stem = _stem(path)
        layer = classify_layer(path)
        seen_layers.add(layer)
        if not stem:
            continue  # cannot match generically-named files — count as unknown, not missed
        reopened = re_searched = surfaced = False
        for m in later:
            blob = m["content"] + " " + m["tool_name"]
            if m["tool_name"] in {"mcp__qmd__get", "mcp__qmd__multi_get", "mcp__qmd__read_resource"} and stem in m["content"]:
                reopened = True
            if m["tool_name"] == "mcp__qmd__query" and stem in m["content"]:
                re_searched = True
            if m["role"] == "assistant" and stem in blob:
                surfaced = True
        if reopened or re_searched or surfaced:
            used.append({"path": path, "layer": layer, "reopened": reopened, "re_searched": re_searched, "surfaced": surfaced})
    return {
        "ts": ev.get("ts"),
        "session_id": ev.get("session_id", "")[:60],
        "channel": ev.get("channel"),
        "query": (ev.get("query") or "")[:70],
        "duration_ms": ev.get("duration_ms"),
        "layers": sorted(seen_layers),
        "n_hits": ev.get("n_hits", 0),
        "n_used": len(used),
        "used": used,
    }


def summarize(scored: list[dict]) -> str:
    if not scored:
        return "no retrieval events to score"
    lines = []
    # overall: how many events produced at least one used hit
    with_use = [s for s in scored if s["n_used"] > 0]
    lines.append(f"events scored: {len(scored)}  | with >=1 used hit: {len(with_use)} ({100*len(with_use)//max(1,len(scored))}%)")
    # per channel × layer: where does useful stuff come from
    buckets: dict[tuple, list] = {}
    for s in scored:
        for lay in s["layers"]:
            buckets.setdefault((s["channel"], lay), []).append(s)
    lines.append("channel     layer      events  used%  p50_ms")
    for (ch, lay), items in sorted(buckets.items()):
        used = sum(1 for s in items if any(u["layer"] == lay for u in s["used"]))
        durs = sorted(s["duration_ms"] or 0 for s in items)
        p50 = durs[len(durs) // 2] if durs else 0
        lines.append(f"{ch or '?':11} {lay:10} {len(items):6}  {100*used//max(1,len(items)):4}   {p50:6}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    events = load_events(args.days)
    scored = []
    # cache messages per session — sessions repeat heavily across turns
    msg_cache: dict[str, list[dict]] = {}
    for ev in events:
        sid = ev.get("session_id") or ""
        if sid not in msg_cache:
            msg_cache[sid] = load_session_messages(sid)
        scored.append(score_event(ev, msg_cache[sid]))

    if args.json:
        print(json.dumps(scored, ensure_ascii=False, indent=1))
    else:
        print(summarize(scored))
        n_filtered = count_filtered(args.days)
        if n_filtered:
            print(f"prefetch events skipped by noise filter (system texts): {n_filtered}")
        # list the misses so a human can judge whether they were truly wasted
        misses = [s for s in scored if s["n_used"] == 0]
        for s in misses[:10]:
            print(f"  no-signal: {s['ts'][:16]} [{s['channel']}] {s['query']!r} layers={s['layers']}")
        if len(misses) > 10:
            print(f"  ...and {len(misses)-10} more no-signal events")


if __name__ == "__main__":
    main()
