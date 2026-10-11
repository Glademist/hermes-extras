#!/usr/bin/env python3
"""dreamer_model_probe.py — cheap-model A/B on the REAL dreamer workload.

Runs the exact production prompt (tiered_anchor_fidelity, the variant
dreamer_v2.run_dreamer_for_session uses) against N candidate models on the
same real sessions, then scores each output structurally. No archive writes,
no state mutation — read-only against state.db, output to stdout.

Scoring is the same hard-signal set dreamer_ab_report.py uses (all six
fields present and non-trivial) plus two quality proxies the shadow report
could not measure: anchor coverage (did the mandatory details actually land
in VÝSLEDEK) and path fidelity (are paths verbatim vs lowercased/ellipsised).

Usage:
  python3 dreamer_model_probe.py --sessions 3
  python3 dreamer_model_probe.py --models deepseek/deepseek-v4-flash,qwen/qwen3.5-9b
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR.parent))
sys.path.insert(0, str(_SCRIPTS_DIR))

from noemem_paths import HERMES_SESSIONS_DIR  # noqa: E402
from session_clean import clean_session  # noqa: E402
from session_source import load_session_content  # noqa: E402
from prompt_variants import dreamer_prompt_variant  # noqa: E402
from projekt_utils import load_catalog_slugs, load_prompt_projects  # noqa: E402
import nous_client as nc  # noqa: E402

FIELDS = ["tema", "proc", "vysledek", "ponauceni", "kategorie", "projekt"]
# Case-insensitive on the home segment: a lowercased path is exactly the defect
# being measured, so PATH_RE must be able to SEE it. Matching only /Users or ~/
# silently dropped every wrong-case path from the comparison.
PATH_RE = re.compile(r"(?:/[Uu]sers|~|/users)/[^\s`,)\"']{3,80}")


def pick_sessions(n: int) -> list[dict]:
    """Newest N discord/telegram sessions with real content, longest-first tier mix."""
    index = json.loads((HERMES_SESSIONS_DIR / "sessions.json").read_text(encoding="utf-8"))
    rows = [
        v for v in index.values()
        if isinstance(v, dict)
        and v.get("platform") in ("discord", "telegram")
        and v.get("session_id")
    ]
    rows.sort(key=lambda v: v.get("updated_at") or "", reverse=True)
    out = []
    for meta in rows:
        raw = load_session_content(meta["session_id"])
        if not raw:
            continue
        clean_text, session_start = clean_session(raw, max_chars=14000, include_tools=True)
        if len(clean_text) < 400:
            continue
        out.append({
            "session_id": meta["session_id"],
            "title": meta.get("display_name", "?")[:70],
            "clean_text": clean_text,
            "session_start": session_start,
        })
        if len(out) >= n:
            break
    return out


def call_model(
    model: str,
    messages: list[dict],
    params: dict,
    timeout: int = 300,
    token_budget: int = 0,
    thinking: bool = False,
) -> dict:
    max_tokens = params.get("max_tokens", 3000)
    if token_budget:
        max_tokens = token_budget
    body = {
        "model": model,
        "messages": messages,
        "temperature": params.get("temperature", 0.15),
        "max_tokens": max_tokens,
    }
    if thinking:
        body["enable_thinking"] = True
    try:
        r = nc.post_chat(body, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"EXC {type(e).__name__}: {e}"[:200]}
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code} {r.text[:180]}"}
    try:
        usage = r.json().get("usage") or {}
    except Exception:  # noqa: BLE001
        usage = {}
    return {
        "ok": True,
        "text": nc.parse_assistant_content(r.json()["choices"][0]["message"]),
        "usage": usage,
    }


def parse_fields(text: str) -> dict:
    """Colon-line parser — the production fields, tolerant of missing lines."""
    out: dict[str, str] = {}
    cur = None
    buf: dict[str, list[str]] = {}
    for line in (text or "").split("\n"):
        s = line.strip()
        up = s.upper()
        hit = None
        for key, label in (("timestamp", "TIMESTAMP:"), ("tema", "TÉMA:"), ("tema", "TEMA:"),
                           ("proc", "PROČ:"), ("proc", "PROC:"),
                           ("vysledek", "VÝSLEDEK:"), ("vysledek", "VYSLEDEK:"),
                           ("ponauceni", "PONAUČENÍ:"), ("ponauceni", "PONAUCENI:"),
                           ("kategorie", "KATEGORIE:"), ("projekt", "PROJEKT:"),
                           ("projekt", "PROJECT:"), ("supersedes", "SUPERSEDES:")):
            if up.startswith(label):
                hit = key
                out[key] = s[len(label):].strip()
                cur = key
                buf[key] = []
                break
        if hit is None and cur and s:
            buf[cur].append(s)
    for k, lines in buf.items():
        if lines:
            out[k] = (out.get(k, "") + " " + " ".join(lines)).strip()
    return out


def score(fields: dict, session_text: str, anchors: list[str], tier_max: int) -> dict:
    missing = [f for f in FIELDS if len(fields.get(f, "").strip()) < 10]
    vysledek = fields.get("vysledek", "")
    pon = fields.get("ponauceni", "")

    # anchor coverage: mandatory details that actually landed in VÝSLEDEK.
    # Compare case-insensitively — that is what production rubrics.py does,
    # and case is scored separately by the path-fidelity check below.
    low = vysledek.lower()
    hit = [a for a in anchors if a.lower()[:28] in low]
    coverage = round(len(hit) / len(anchors), 2) if anchors else None

    # Path fidelity. The session's own paths may themselves be lowercased
    # (that was the L6 defect), so the reference set is keyed case-insensitively
    # and each output path is classified against it:
    #   verbatim      — same characters, same case
    #   lowercased    — exists in session only when both are lowercased
    #   wrong_case    — exists in session with different case (the real defect)
    #   fabricated    — not in the session at all
    sess_by_lower: dict[str, str] = {}
    for p in PATH_RE.findall(session_text):
        p = p.rstrip(".,;")
        sess_by_lower.setdefault(p.lower(), p)
    verbatim = wrong_case = fabricated = 0
    for p in {q.rstrip(".,;") for q in PATH_RE.findall(vysledek + " " + pon)}:
        ref = sess_by_lower.get(p.lower())
        if ref is None:
            fabricated += 1
        elif ref == p:
            verbatim += 1
        else:
            wrong_case += 1
    ellipsised = sum(1 for p in PATH_RE.findall(vysledek + " " + pon) if "..." in p or "…" in p)

    # noise markers the prompt explicitly bans
    noise = sum(vysledek.count(t) for t in (
        "call_", "TOOL[", "pip install", "0.68", "venv", "Clone", "clone of",
    ))

    sentences = [s for s in re.split(r"(?<=[.!?])\s+", vysledek) if s.strip()]
    return {
        "complete": not missing,
        "missing": missing,
        "tema": fields.get("tema", "")[:70],
        "projekt": fields.get("projekt", "")[:40],
        "kategorie": fields.get("kategorie", "")[:20],
        "vysledek_chars": len(vysledek),
        "ponauceni_chars": len(pon),
        "vysledek_sentences": len(sentences),
        "tier_sentence_cap": tier_max,
        "anchor_coverage": coverage,
        "anchors_hit": f"{len(hit)}/{len(anchors)}",
        "paths_out": verbatim + wrong_case + fabricated,
        "paths_verbatim": verbatim,
        "paths_wrong_case": wrong_case,
        "paths_fabricated": fabricated,
        "paths_ellipsised": ellipsised,
        "noise_markers": noise,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=3)
    ap.add_argument("--models", default=(
        "deepseek/deepseek-v4-flash,"
        "inclusionai/ling-3.0-flash-sante:free,"
        "stepfun/step-3.7-flash:free,"
        "upstage/solar-pro4:free"
    ))
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--max-tokens", type=int, default=0,
                    help="override the prompt's max_tokens (0 = use prompt value)")
    ap.add_argument("--thinking", action="store_true",
                    help="send enable_thinking=true (production default for nous)")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    sessions = pick_sessions(args.sessions)
    if not sessions:
        print("no sessions with content found")
        return 1

    known_projects = sorted(set(load_catalog_slugs()) | set(load_prompt_projects()))
    print(f"🔬 Dreamer model probe — {len(sessions)} real sessions × {len(models)} models")
    print(f"   prompt variant: tiered_anchor_fidelity (production)")
    for s in sessions:
        print(f"   · {s['session_id']} {len(s['clean_text'])} chars — {s['title']}")
    print()

    results: dict[str, list[dict]] = {m: [] for m in models}

    for s in sessions:
        fixture = {
            "clean_text": s["clean_text"],
            "session_start": s["session_start"],
            "char_count": len(s["clean_text"]),
        }
        messages, params = dreamer_prompt_variant(fixture, "tiered_anchor_fidelity", known_projects)
        from dreamer_tiers import session_tier
        tier = session_tier(len(s["clean_text"]))
        cap = 4 if "3" in tier.vysledek_vety_prompt and "4" in tier.vysledek_vety_prompt else 0
        from prompt_variants import _prompt_anchors
        anchors = _prompt_anchors(s["clean_text"], n=tier.inject_anchor_count)

        print(f"📌 {s['session_id']} — tier={tier.label}, {len(anchors)} anchors injected")
        for m in models:
            r = call_model(m, messages, params, timeout=args.timeout,
                           token_budget=args.max_tokens, thinking=args.thinking)
            if not r["ok"]:
                print(f"   ❌ {m}: {r['error']}")
                results[m].append({"model": m, "ok": False, "error": r["error"]})
                continue
            f = parse_fields(r["text"])
            sc = score(f, s["clean_text"], anchors, cap)
            sc["model"] = m
            sc["ok"] = True
            sc["raw_len"] = len(r["text"] or "")
            sc["usage"] = r.get("usage") or {}
            results[m].append(sc)
            print(f"   ✅ {m}")
            print(f"      complete={sc['complete']} missing={sc['missing'] or '—'}")
            print(f"      VÝSLEDEK {sc['vysledek_chars']}ch/{sc['vysledek_sentences']}sent | "
                  f"PONAUČENÍ {sc['ponauceni_chars']}ch | anchors {sc['anchors_hit']}")
            print(f"      paths: {sc['paths_verbatim']} verbatim / {sc['paths_wrong_case']} wrong-case / "
                  f"{sc['paths_fabricated']} fabricated / {sc['paths_ellipsised']} ellipsised | "
                  f"noise {sc['noise_markers']}")
            print(f"      TÉMA: {sc['tema']}")
            print(f"      PROJEKT: {sc['projekt']}  ({sc['kategorie']})")
        print()

    # ── summary ──
    print("=" * 72)
    print("SUMMARY (production prompt, real sessions)")
    print("=" * 72)
    for m in models:
        rows = [r for r in results[m] if r.get("ok")]
        if not rows:
            print(f"\n{m}: no successful runs")
            continue
        n = len(rows)
        ok = sum(1 for r in rows if r["complete"])
        avg_v = round(sum(r["vysledek_chars"] for r in rows) / n)
        avg_p = round(sum(r["ponauceni_chars"] for r in rows) / n)
        cov = [r["anchor_coverage"] for r in rows if r["anchor_coverage"] is not None]
        avg_cov = round(sum(cov) / len(cov), 2) if cov else None
        pv = sum(r["paths_verbatim"] for r in rows)
        pw = sum(r["paths_wrong_case"] for r in rows)
        pf = sum(r["paths_fabricated"] for r in rows)
        pe = sum(r["paths_ellipsised"] for r in rows)
        noise = sum(r["noise_markers"] for r in rows)
        pt = sum(r.get("usage", {}).get("prompt_tokens", 0) or 0 for r in rows)
        ct = sum(r.get("usage", {}).get("completion_tokens", 0) or 0 for r in rows)
        print(f"\n{m}")
        print(f"  structure complete : {ok}/{n}")
        print(f"  VÝSLEDEK avg       : {avg_v} chars")
        print(f"  PONAUČENÍ avg      : {avg_p} chars")
        print(f"  anchor coverage    : {avg_cov}")
        print(f"  paths              : {pv} verbatim / {pw} wrong-case / {pf} fabricated / {pe} ellipsised")
        print(f"  noise markers      : {noise}")
        print(f"  tokens             : {pt} in / {ct} out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
