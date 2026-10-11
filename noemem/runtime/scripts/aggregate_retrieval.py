#!/usr/bin/env python3
"""Aggregate QMD retrieval JSONL telemetry (Fáze 1 baseline).

Usage:
  python3 aggregate_retrieval.py
  python3 aggregate_retrieval.py --days 7
  python3 aggregate_retrieval.py --month 2026-07
  python3 aggregate_retrieval.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

LOG_DIR = Path.home() / "hermes-noemem" / "logs"
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# # lightweight tokenize for empty-query clustering
_WORD_RE = re.compile(r"[a-zA-ZáčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9_./:-]{3,}")


def _parse_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def iter_events(
    month: Optional[str] = None,
    days: Optional[int] = None,
) -> Iterable[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days) if days else None
    files: List[Path] = []
    if month:
        p = LOG_DIR / f"qmd-retrieval-{month}.jsonl"
        if p.exists():
            files = [p]
    else:
        files = sorted(LOG_DIR.glob("qmd-retrieval-*.jsonl"))
        if days is not None:
            # include current + previous month always for week windows
            files = files[-3:]

    for path in files:
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if cutoff is not None:
                        ts = _parse_ts(str(ev.get("ts") or ""))
                        if ts is not None and ts < cutoff:
                            continue
                    yield ev
        except OSError:
            continue


def _norm_query(q: str) -> str:
    q = (q or "").strip().lower()
    q = re.sub(r"\s+", " ", q)
    return q[:160]


# Prefetch noise patterns — system/not-user messages that QMD can't and
# shouldn't match. Excluded from the "clean" empty-rate so the metric reflects
# real conversational searches. Add patterns here to extend.
_NOISE_RE = [
    re.compile(r"^\s*cronjob response", re.IGNORECASE),
    re.compile(r"^\s*\[important: background process", re.IGNORECASE),
    # agent-echo of a cron delivery: "[the agent [bot]] Cronjob Response: ..."
    re.compile(r"^\s*\[you\s*\[bot\]\]\s*cronjob response", re.IGNORECASE),
]


def _is_noise_query(q: str) -> bool:
    return any(pat.search(q or "") for pat in _NOISE_RE)


def _query_cluster_key(q: str) -> str:
    toks = _WORD_RE.findall((q or "").lower())
    # drop ultra-common noise
    stop = {"the", "and", "pro", "jak", "co", "je", "na", "to", "with", "from", "that"}
    keep = [t for t in toks if t not in stop][:8]
    return " ".join(keep) if keep else _norm_query(q)[:80]


def aggregate(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(events)
    by_channel = Counter()
    empty_by_channel = Counter()
    noise = 0
    layer = Counter()
    tools = Counter()
    durations: List[float] = []
    empty_clusters: Counter = Counter()
    non_empty_clusters: Counter = Counter()
    errors = 0
    score_bins = Counter()

    for ev in events:
        ch = str(ev.get("channel") or "unknown")
        q = str(ev.get("query") or "")
        empty = bool(ev.get("empty"))
        if empty and _is_noise_query(q):
            noise += 1
            continue  # system noise — excluded from empty-rate entirely
        by_channel[ch] += 1
        if empty:
            empty_by_channel[ch] += 1
            empty_clusters[_query_cluster_key(q)] += 1
        else:
            non_empty_clusters[_query_cluster_key(q)] += 1
        layer[str(ev.get("layer_top1") or "none")] += 1
        if ev.get("tool_name"):
            tools[str(ev.get("tool_name"))] += 1
        if ev.get("error"):
            errors += 1
        d = ev.get("duration_ms")
        if isinstance(d, (int, float)):
            durations.append(float(d))
        # top hit score
        hits = ev.get("hits") or []
        if isinstance(hits, list) and hits:
            sc = hits[0].get("score") if isinstance(hits[0], dict) else None
            if isinstance(sc, (int, float)):
                if sc >= 0.7:
                    score_bins[">=0.7"] += 1
                elif sc >= 0.4:
                    score_bins["0.4-0.7"] += 1
                else:
                    score_bins["<0.4"] += 1

    def rate(n: int, d: int) -> Optional[float]:
        return round(n / d, 4) if d else None

    empty_total = sum(empty_by_channel.values())
    clean_total = total - noise  # exclude system-noise events from the rate
    raw_total = sum(empty_by_channel.values()) + noise  # all empty incl. excluded noise
    dur_sorted = sorted(durations)
    p50 = dur_sorted[len(dur_sorted) // 2] if dur_sorted else None
    p95 = (
        dur_sorted[int(len(dur_sorted) * 0.95)]
        if len(dur_sorted) >= 2
        else (dur_sorted[0] if dur_sorted else None)
    )

    return {
        "n_events": total,
        "by_channel": dict(by_channel),
        "empty_total": empty_total,
        "empty_rate": rate(empty_total, clean_total) if clean_total else None,
        "empty_rate_raw": rate(raw_total, total) if total else None,
        "noise_excluded": noise,
        "empty_rate_by_channel": {
            ch: rate(empty_by_channel[ch], by_channel[ch]) for ch in by_channel
        },
        "layer_top1": dict(layer),
        "layer_top1_share": {
            k: rate(v, total) for k, v in layer.items()
        },
        "tools": dict(tools),
        "errors": errors,
        "duration_ms": {"p50": p50, "p95": p95, "n": len(durations)},
        "top_hit_score_bins": dict(score_bins),
        "top_empty_query_clusters": empty_clusters.most_common(20),
        "top_non_empty_query_clusters": non_empty_clusters.most_common(15),
    }


def format_report(agg: Dict[str, Any], *, days: Optional[int], month: Optional[str]) -> str:
    window = f"month={month}" if month else (f"last {days}d" if days else "all available logs")
    lines = [
        f"# QMD retrieval aggregate ({window})",
        f"generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        f"- events: **{agg['n_events']}**",
        f"- empty_rate (clean, excl. noise): **{agg['empty_rate']}** ({agg['empty_total']} empty)",
        f"- empty_rate raw (incl. noise): {agg.get('empty_rate_raw')}",
        f"- noise_excluded: {agg.get('noise_excluded')} (system messages — Cronjob Response / Background process)",
        f"- errors: {agg['errors']}",
        f"- duration_ms p50/p95: {agg['duration_ms'].get('p50')} / {agg['duration_ms'].get('p95')}",
        "",
        "## By channel",
    ]
    for ch, n in sorted(agg["by_channel"].items()):
        er = (agg.get("empty_rate_by_channel") or {}).get(ch)
        lines.append(f"- `{ch}`: {n} (empty_rate={er})")
    lines += ["", "## layer_top1"]
    for k, v in sorted((agg.get("layer_top1") or {}).items(), key=lambda kv: -kv[1]):
        share = (agg.get("layer_top1_share") or {}).get(k)
        lines.append(f"- `{k}`: {v} (share={share})")
    if agg.get("tools"):
        lines += ["", "## MCP tools"]
        for k, v in sorted(agg["tools"].items(), key=lambda kv: -kv[1]):
            lines.append(f"- `{k}`: {v}")
    if agg.get("top_hit_score_bins"):
        lines += ["", "## Top-hit score bins (non-parsed scores may be absent)"]
        for k, v in sorted(agg["top_hit_score_bins"].items()):
            lines.append(f"- {k}: {v}")
    lines += ["", "## Top empty query clusters"]
    for q, n in agg.get("top_empty_query_clusters") or []:
        lines.append(f"- ({n}) {q or '∅'}")
    lines += ["", "## Top non-empty query clusters"]
    for q, n in agg.get("top_non_empty_query_clusters") or []:
        lines.append(f"- ({n}) {q or '∅'}")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=None, help="Only events newer than N days")
    ap.add_argument("--month", type=str, default=None, help="YYYY-MM log file only")
    ap.add_argument("--json", action="store_true", help="Print JSON instead of markdown")
    ap.add_argument(
        "--write",
        type=str,
        default="",
        help="Optional path to write markdown report",
    )
    args = ap.parse_args(argv)

    events = list(iter_events(month=args.month, days=args.days))
    agg = aggregate(events)

    if args.json:
        print(json.dumps(agg, ensure_ascii=False, indent=2))
    else:
        report = format_report(agg, days=args.days, month=args.month)
        print(report)
        if args.write:
            out = Path(args.write).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(report, encoding="utf-8")
            print(f"\n# wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
