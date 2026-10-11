#!/usr/bin/env python3
"""Quality rubrics for dreamer and nightly outputs."""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

from dreamer_tiers import TierSpec, is_garbage_anchor, session_tier


def parse_dreamer_output(text: str) -> Optional[dict]:
    result = {
        "timestamp": "",
        "tema": "",
        "proc": "",
        "vysledek": "",
        "ponauceni": "",
        "kategorie": "",
        "projekt": "",
        "supersedes": "",
    }
    lines = text.strip().split("\n")
    current_field = None
    field_buffers = {k: [] for k in result}

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("TIMESTAMP:"):
            result["timestamp"] = stripped[10:].strip()
            current_field = "timestamp"
        elif stripped.startswith("TÉMA:") or stripped.startswith("TEMA:"):
            result["tema"] = stripped.split(":", 1)[1].strip()
            current_field = "tema"
        elif stripped.startswith("PROČ:") or stripped.startswith("PROC:"):
            result["proc"] = stripped.split(":", 1)[1].strip()
            current_field = "proc"
        elif stripped.startswith("VÝSLEDEK:") or stripped.startswith("VYSLEDEK:"):
            current_field = "vysledek"
            rest = stripped.split(":", 1)[1].strip()
            if rest:
                field_buffers["vysledek"].append(rest)
        elif stripped.startswith("PONAUČENÍ:") or stripped.startswith("PONAUCENI:"):
            current_field = "ponauceni"
            rest = stripped.split(":", 1)[1].strip()
            if rest:
                field_buffers["ponauceni"].append(rest)
        elif stripped.startswith("KATEGORIE:"):
            result["kategorie"] = stripped[10:].strip().lower()
            current_field = "kategorie"
        elif stripped.startswith("PROJEKT:"):
            result["projekt"] = stripped[8:].strip()
            current_field = "projekt"
        elif stripped.startswith("SUPERSEDES:") or stripped.startswith("SUPERSEDE:"):
            result["supersedes"] = stripped.split(":", 1)[1].strip()
            current_field = "supersedes"
        elif current_field in ("vysledek", "ponauceni") and stripped:
            field_buffers[current_field].append(stripped)

    for key in ("vysledek", "ponauceni"):
        if field_buffers[key]:
            result[key] = "\n".join(field_buffers[key])

    if not result["tema"] and not result["proc"] and not result["vysledek"]:
        return None
    if result["kategorie"] not in ("personal", "projects", "maintenance", "knowledge"):
        result["kategorie"] = "personal"
    return result


def czech_ratio(text: str) -> float:
    if not text:
        return 0.0
    czech_chars = sum(1 for c in text if c in "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ")
    letters = sum(1 for c in text if c.isalpha())
    return (czech_chars / letters) if letters else 0.0


def detect_thinking_in_content(text: str) -> bool:
    markers = (
        "here's a thinking process",
        "thinking process:",
        "let me think",
        "thought process",
        "<think>",
        "analysis:",
    )
    lower = text.lower()[:800]
    return any(m in lower for m in markers)


def _sentence_count(text: str) -> int:
    """Count sentences without splitting on IPs, decimals, or dotted code tokens."""
    protected = re.sub(
        r"`[^`]+`",
        lambda m: m.group(0).replace(".", "·"),
        text,
    )
    protected = re.sub(
        r"\b\d{1,3}(?:\.\d{1,3}){3}\b",
        lambda m: m.group(0).replace(".", "·"),
        protected,
    )
    protected = re.sub(r"(?<=\d)\.(?=\d)", "·", protected)
    parts = re.split(r"[.!?…]+", protected)
    return len([p for p in parts if p.strip()])


def _session_anchor_tokens(session_text: str, *, filter_garbage: bool = True) -> set[str]:
    """Cheap anchors from session for substance checks.

    Case is PRESERVED: anchors are injected into the prompt as mandatory
    backtick-quoted details, so `.lower()` here ships lowercase paths into
    VÝSLEDEK. Lowercasing is for MATCHING only (compare in the caller).
    """
    anchors: set[str] = set()
    for m in re.finditer(r"`([^`]{3,80})`", session_text):
        a = m.group(1).strip()
        if filter_garbage and is_garbage_anchor(a):
            continue
        if filter_garbage and re.fullmatch(r"[a-záčďéěíňóřšťúůýž]+", a) and len(a) < 8:
            continue
        anchors.add(a)
    for m in re.finditer(r"/(?:Users|\.hermes|Projekty)[^\s`\"']{3,60}", session_text):
        anchors.add(m.group(0))
    for m in re.finditer(r"\b(?:mtplx|dreamer|qmd|hermes-noemem|launchctl|state\.db)\b", session_text, re.I):
        anchors.add(m.group(0))
    return anchors


def _strip_prompt_preamble(text: str) -> str:
    lines = text.strip().split("\n")
    while lines and (
        lines[0].strip().upper().startswith("FAKTA:")
        or (not lines[0].strip() and len(lines) > 1)
    ):
        lines.pop(0)
    return "\n".join(lines)


def score_dreamer_production(
    text: str,
    session_text: str = "",
    *,
    char_count: int | None = None,
    injected_anchors: list[str] | None = None,
    tier: TierSpec | None = None,
) -> dict:
    """Absolute production gate — structure + substance, no reference model."""
    base = score_dreamer(_strip_prompt_preamble(text))
    if not base.get("parse_ok"):
        return {**base, "production_score": 0, "production_pass": False}

    if tier is None and char_count is not None:
        tier = session_tier(char_count)
    elif tier is None and session_text:
        tier = session_tier(len(session_text))

    parsed = base["parsed"] or {}
    score = 0
    notes = list(base.get("notes") or [])
    max_score = 100

    # Structure (55)
    vys = parsed.get("vysledek", "")
    pon = parsed.get("ponauceni", "")
    v_sent = _sentence_count(vys)
    p_sent = _sentence_count(pon)
    v_min, v_max = (tier.vysledek_min, tier.vysledek_max) if tier else (2, 6)
    p_min, p_max = (tier.ponauceni_min, tier.ponauceni_max) if tier else (1, 4)
    if v_min <= v_sent <= v_max:
        score += 15
    else:
        notes.append(f"vysledek_sentences:{v_sent}")
    if p_min <= p_sent <= p_max:
        score += 10
    else:
        notes.append(f"ponauceni_sentences:{p_sent}")
    if len(vys) >= 120:
        score += 15
    elif len(vys) >= 60:
        score += 8
    else:
        notes.append("thin_vysledek")
    if len(pon) >= 50:
        score += 10
    elif len(pon) >= 25:
        score += 5
    else:
        notes.append("thin_ponauceni")
    proj = _canonical_projekt(parsed.get("projekt", ""))
    if proj and not re.search(r"-\d{8}$", proj):
        score += 5
    else:
        notes.append("noncanonical_projekt")

    # Substance (35) — injected anchors preferred over full session scan
    if injected_anchors:
        anchors = {a.lower() for a in injected_anchors if a}
    elif session_text:
        anchors = _session_anchor_tokens(session_text)
    else:
        anchors = set()

    if anchors:
        out_lower = (vys + " " + pon + " " + parsed.get("proc", "")).lower()
        hit = sum(1 for a in anchors if a in out_lower)
        ratio = hit / len(anchors)
        score += int(ratio * 35)
        if ratio < 0.15:
            notes.append("low_substance_overlap")
        if injected_anchors and ratio < 0.8:
            notes.append(f"injected_anchor_miss:{len(anchors) - hit}")
    else:
        score += 20

    production_pass = (
        base.get("quality_score", 0) >= 85
        and score >= 70
        and "thinking_leaked_into_content" not in notes
        and "parse_failed" not in notes
        and v_sent >= v_min
        and p_sent >= p_min
    )

    return {
        **base,
        "production_score": min(max_score, score),
        "production_pass": production_pass,
        "notes": notes,
        "tier": tier.label if tier else None,
    }


def score_dreamer(text: str) -> dict:
    parsed = parse_dreamer_output(text)
    score = 0
    notes: list[str] = []
    if parsed is None:
        return {"parse_ok": False, "quality_score": 0, "parsed": None, "notes": ["parse_failed"]}

    score += 40
    required = ["tema", "proc", "vysledek", "ponauceni", "kategorie", "projekt"]
    for field in required:
        if parsed.get(field):
            score += 5
        else:
            notes.append(f"missing_{field}")

    if len(parsed.get("vysledek", "")) >= 40:
        score += 7
    else:
        notes.append("short_vysledek")
    if len(parsed.get("ponauceni", "")) >= 40:
        score += 8
    else:
        notes.append("short_ponauceni")

    if parsed.get("kategorie") in ("personal", "projects", "maintenance", "knowledge"):
        score += 5

    if czech_ratio(text) >= 0.02 or any(w in text.lower() for w in ("projekt", "session", "téma", "ponaučení")):
        score += 10
    else:
        notes.append("low_czech")

    if detect_thinking_in_content(text):
        notes.append("thinking_leaked_into_content")
        score = max(0, score - 25)

    return {
        "parse_ok": True,
        "quality_score": min(100, score),
        "parsed": parsed,
        "thinking_in_content": detect_thinking_in_content(text),
        "notes": notes,
    }


def score_nightly(text: str, *, mode: str = "daily") -> dict:
    if not text or not text.strip():
        return {"parse_ok": False, "quality_score": 0, "notes": ["empty"]}
    if mode == "weekly":
        return _score_nightly_weekly(text)
    score = 20
    notes: list[str] = []
    if "### Session" in text and "Cíle" in text:
        score += 25
    else:
        notes.append("missing_session_cile")
    if "### Změny v cílech" in text:
        score += 20
    else:
        notes.append("missing_zmeny")
    if "### Návrhy ke schválení" in text:
        score += 25
    else:
        notes.append("missing_navrhy")
    if re.search(r"\[G[1-9]\]|\[NOVÝ\]|\[NOVY\]", text):
        score += 10
    else:
        notes.append("missing_goal_tags")
    return {
        "parse_ok": score >= 70,
        "quality_score": min(100, score),
        "notes": notes,
    }


def _score_nightly_weekly(text: str) -> dict:
    markers = (
        "### Přiřazení",
        "### Nové cíle",
        "### Změny priorit",
        "### Návrhy do _vztahovy",
        "### Návrhy do goal",
    )
    score = 10
    notes: list[str] = []
    per = 14
    for marker in markers:
        if marker in text:
            score += per
        else:
            notes.append(f"missing:{marker}")
    if re.search(r"\[G[1-9]\]|\[NOVÝ\]|\[NOVY\]", text):
        score += 10
    else:
        notes.append("missing_goal_tags")
    return {
        "parse_ok": score >= 70,
        "quality_score": min(100, score),
        "notes": notes,
    }


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _canonical_projekt(s: str) -> str:
    s = (s or "").strip()
    if not s or s.upper().startswith("NOV"):
        return s
    while re.search(r"-\d{8}$", s):
        s = re.sub(r"-\d{8}$", "", s)
    return s


def compare_dreamer_to_reference(candidate: dict | None, reference: dict | None) -> dict:
    if not candidate or not reference:
        return {"match_score": 0, "notes": ["missing_parsed"]}
    score = 0
    notes: list[str] = []
    if _norm(candidate.get("kategorie", "")) == _norm(reference.get("kategorie", "")):
        score += 20
    else:
        notes.append(f"kategorie:{candidate.get('kategorie')}!=ref:{reference.get('kategorie')}")
    if _norm(_canonical_projekt(candidate.get("projekt", ""))) == _norm(
        _canonical_projekt(reference.get("projekt", ""))
    ):
        score += 30
    else:
        notes.append("projekt_diff")
    ref_words = set(_norm(reference.get("tema", "")).split())
    cand_words = set(_norm(candidate.get("tema", "")).split())
    if ref_words and cand_words:
        overlap = len(ref_words & cand_words) / len(ref_words)
        score += int(overlap * 30)
        if overlap < 0.3:
            notes.append("tema_low_overlap")
    if len(candidate.get("vysledek", "")) >= max(40, len(reference.get("vysledek", "")) * 0.4):
        score += 20
    else:
        notes.append("thin_vysledek_vs_ref")
    return {"match_score": min(100, score), "notes": notes}


def compare_nightly_to_reference(candidate: str, reference: str) -> dict:
    if not reference.strip():
        return {"match_score": 0, "notes": ["no_reference"]}
    score = 0
    notes: list[str] = []
    for section in ("### Session", "### Změny", "### Návrhy"):
        if section in candidate and section in reference:
            score += 20
        else:
            notes.append(f"section_mismatch:{section}")
    ref_tags = set(re.findall(r"\[G\d+\]|\[NOVÝ\]|\[NOVY\]", reference))
    cand_tags = set(re.findall(r"\[G\d+\]|\[NOVÝ\]|\[NOVY\]", candidate))
    if ref_tags and cand_tags & ref_tags:
        score += 20
    elif ref_tags:
        notes.append("goal_tag_mismatch")
    ref_kw = {w for w in _norm(reference).split() if len(w) > 5}
    cand_kw = {w for w in _norm(candidate).split() if len(w) > 5}
    if ref_kw:
        score += int(min(1.0, len(ref_kw & cand_kw) / len(ref_kw)) * 20)
    return {"match_score": min(100, score), "notes": notes}