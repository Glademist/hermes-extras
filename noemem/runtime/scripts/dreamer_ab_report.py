#!/usr/bin/env python3
"""dreamer_ab_report.py — cloud (nous deepseek) vs gemma on the dreamer workload.

Production noemem archives are written by the cloud backend (LLM_BACKEND=nous,
the default since Nex was disabled 2026-09-22). The gemma shadow leg
(gemma_shadow_run.sh, daily 04:30) writes the SAME sessions into
hermes-noemem-sandbox-gemma/archive/. This script pairs archives by
source_session_id and scores both sides structurally — no LLM judging, just the
hard signals: frontmatter completeness (TÉMA/PROČ/VÝSLEDEK/PONAUČENÍ/KATEGORIE/
PROJEKT), importance present, body length, PONAUČENÍ quality marker.

Silent until `--min-pairs` paired archives exist AND `--new-since-last` new ones
arrived since the previous report (state file) — cron-safe.

Usage:
  python3 dreamer_ab_report.py                 # gated report
  python3 dreamer_ab_report.py --force         # print even below threshold
  python3 dreamer_ab_report.py --pairs 3       # dump the 3 newest pairs side-by-side
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

PROD = Path("~/hermes-noemem/archive")
SHADOW = Path("~/hermes-noemem-sandbox-gemma/archive")
STATE = Path("~/hermes-noemem/state/dreamer-ab-report.json")
MD = Path("~/hermes-noemem/logs/dreamer-ab-report.md")

FIELDS = ["TÉMA:", "PROČ:", "VÝSLEDEK:", "PONAUČENÍ:", "KATEGORIE:", "PROJEKT:"]


def ascii_key(field: str) -> str:
    stripped = unicodedata.normalize("NFKD", field.rstrip(":").lower())
    return "".join(c for c in stripped if not unicodedata.combining(c))


def parse_archive(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    sid_m = re.search(r"source_session_id:\s*(\S+)", text)
    body = text.split("---", 2)[-1]
    out = {
        "file": path.name,
        "session_id": sid_m.group(1) if sid_m else None,
        "chars": len(text),
    }
    for f in FIELDS:
        key = ascii_key(f)
        m = re.search(rf"^{re.escape(f)}\s*(.+)$", body, re.M)
        val = (m.group(1).strip() if m else "") or ""
        out[key] = val
        out[key + "_len"] = len(val)
    out["complete"] = all(out[ascii_key(f) + "_len"] > 10 for f in FIELDS)
    imp = re.search(r"importance:\s*(\d+)", text)
    out["importance"] = int(imp.group(1)) if imp else None
    return out


def load_all() -> list[dict]:
    pairs = []
    prod_by_sid = {}
    for p in PROD.glob("*.md"):
        a = parse_archive(p)
        if a["session_id"]:
            prod_by_sid[a["session_id"]] = a
    for p in SHADOW.glob("*.md"):
        a = parse_archive(p)
        sid = a["session_id"]
        if sid and sid in prod_by_sid:
            pairs.append({"session_id": sid, "cloud": prod_by_sid[sid], "gemma": a})
    pairs.sort(key=lambda x: x["cloud"]["file"], reverse=True)
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-pairs", type=int, default=8)
    ap.add_argument("--new-since-last", type=int, default=3)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--pairs", type=int, default=4, help="newest pairs to show side-by-side")
    args = ap.parse_args()

    pairs = load_all()

    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except Exception:
            state = {}
    reported = int(state.get("reported_pairs", 0))

    if len(pairs) < args.min_pairs or (len(pairs) - reported) < args.new_since_last:
        if args.force:
            print(f"dreamer A/B: {len(pairs)} paired archives ({len(pairs) - reported} new) — below threshold "
                  f"({args.min_pairs} total, +{args.new_since_last} new)")
        return 0

    def score(side: dict) -> dict:
        return {
            "complete": side["complete"],
            "chars": side["chars"],
            "ponauceni": side[ascii_key("PONAUČENÍ") + "_len"],
        }

    n = len(pairs)
    cloud_ok = sum(1 for p in pairs if p["cloud"]["complete"])
    gemma_ok = sum(1 for p in pairs if p["gemma"]["complete"])
    cloud_chars = [p["cloud"]["chars"] for p in pairs]
    gemma_chars = [p["gemma"]["chars"] for p in pairs]
    avg = lambda xs: round(sum(xs) / len(xs)) if xs else 0  # noqa: E731
    thin_gemma = [p for p in pairs if p["gemma"]["chars"] < 0.5 * p["cloud"]["chars"]]

    lines = [
        f"🔬 Dreamer A/B — cloud (deepseek) vs gemma — {n} paired archives",
        "",
        f"• cloud: structure complete {cloud_ok}/{n}, avg {avg(cloud_chars)} chars",
        f"• gemma: structure complete {gemma_ok}/{n}, avg {avg(gemma_chars)} chars",
    ]
    if thin_gemma:
        lines.append(f"• ⚠️ gemma much thinner (<50 % of cloud length): "
                     f"{', '.join(p['gemma']['file'][:60] for p in thin_gemma[:4])}"
                     + (f" … +{len(thin_gemma) - 4}" if len(thin_gemma) > 4 else ""))
    lines.append("")
    for p in pairs[: args.pairs]:
        tema = ascii_key("TÉMA")
        pon = ascii_key("PONAUČENÍ")
        lines.append(f"## {p['cloud']['file']}")
        lines.append(f"- cloud:  TÉMA: {p['cloud'][tema][:130]!r}")
        lines.append(f"- gemma:  TÉMA: {p['gemma'][tema][:130]!r}")
        lines.append(f"- PONAUČENÍ cloud:  {p['cloud'][pon][:160]!r}")
        lines.append(f"- PONAUČENÍ gemma:  {p['gemma'][pon][:160]!r}")
        lines.append("")

    report = "\n".join(lines) + f"\n(detail: pairs dumped from {PROD} vs {SHADOW})\n"
    print(report)

    old = MD.read_text(encoding="utf-8") if MD.exists() else ""
    MD.parent.mkdir(parents=True, exist_ok=True)
    MD.write_text(report + "\n---\n\n" + old, encoding="utf-8")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(
        {"reported_pairs": n, "ts": datetime.now().astimezone().isoformat(timespec="seconds")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
