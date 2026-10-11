#!/usr/bin/env python3
"""Session size tiers for dreamer prompting and scoring."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Align with session_suite.pick_stratified_suite bucket boundaries.
SHORT_MAX = 2500
MEDIUM_MAX = 6000
LONG_MAX = 10000
MAX_SESSION_CHARS = 14000


@dataclass(frozen=True)
class TierSpec:
    label: str
    vysledek_min: int
    vysledek_max: int
    ponauceni_min: int
    ponauceni_max: int
    vysledek_vety_prompt: str
    ponauceni_vety_prompt: str
    inject_anchor_count: int
    multipass: bool
    write_max_tokens: int
    extract_max_tokens: int = 2000


TIERS: dict[str, TierSpec] = {
    "short": TierSpec(
        label="short",
        vysledek_min=2,
        vysledek_max=6,
        ponauceni_min=1,
        ponauceni_max=4,
        vysledek_vety_prompt="exactly 3 or 4 sentences",
        ponauceni_vety_prompt="exactly 2 sentences",
        inject_anchor_count=6,
        multipass=False,
        write_max_tokens=2000,
    ),
    "medium": TierSpec(
        label="medium",
        vysledek_min=2,
        vysledek_max=7,
        ponauceni_min=1,
        ponauceni_max=4,
        vysledek_vety_prompt="4 to 5 sentences",
        ponauceni_vety_prompt="exactly 2 sentences",
        inject_anchor_count=8,
        multipass=False,
        write_max_tokens=2500,
    ),
    "long": TierSpec(
        label="long",
        vysledek_min=2,
        vysledek_max=8,
        ponauceni_min=1,
        ponauceni_max=4,
        vysledek_vety_prompt="5 to 6 sentences",
        ponauceni_vety_prompt="2 to 3 sentences",
        inject_anchor_count=10,
        multipass=False,
        write_max_tokens=2800,
    ),
    "xl": TierSpec(
        label="xl",
        vysledek_min=2,
        vysledek_max=10,
        ponauceni_min=1,
        ponauceni_max=4,
        vysledek_vety_prompt="6 to 8 sentences",
        ponauceni_vety_prompt="exactly 3 sentences",
        inject_anchor_count=12,
        multipass=True,
        write_max_tokens=3000,
        extract_max_tokens=2200,
    ),
}


def session_char_count(fixture_or_text: dict[str, Any] | str) -> int:
    if isinstance(fixture_or_text, dict):
        if fixture_or_text.get("char_count"):
            return int(fixture_or_text["char_count"])
        return len(fixture_or_text.get("clean_text", "") or "")
    return len(fixture_or_text or "")


def session_tier(char_count: int) -> TierSpec:
    if char_count < SHORT_MAX:
        return TIERS["short"]
    if char_count < MEDIUM_MAX:
        return TIERS["medium"]
    if char_count < LONG_MAX:
        return TIERS["long"]
    return TIERS["xl"]


def tier_size_preamble(tier: TierSpec, char_count: int) -> str:
    return f"""
Session size: {tier.label} ({char_count} characters).
- RESULT: {tier.vysledek_vety_prompt} — every sentence must contain at least one concrete artifact in `backticks`.
- LESSON: {tier.ponauceni_vety_prompt}.
- This is NOT a chat summary; it is a searchable record for future retrieval (paths, commands, numbers)."""


def is_garbage_anchor(a: str) -> bool:
    """Skip markdown/session fragments that cause garbled VÝSLEDEK copies."""
    if not a or "\n" in a or len(a) > 90:
        return True
    # Editor rework 2026-09-21: anchors force facts into VÝSLEDEK, so only
    # DURABLE tokens may anchor. Transient tooling/venv/pip trivia fails the
    # next-month test and must not be forced verbatim into the record.
    if re.search(
        r"(?:^|[/\s.`\"'-])(?:\.venv|venv|pip\d*|pip3?|activate|site-packages|"
        r"python3?\.\d+|python3?|__pycache__|\.cache|/tmp/|/private/var|"
        r"silenceremove|ffmpeg)(?:[/\s.`\"'-]|$)",
        a,
        re.I,
    ):
        return True
    if "\\n" in a or a.endswith("\\"):  # literal escaped newline from JSON session text
        return True
    if re.search(
        r"\*\*|:\*\*|shrnutí|xml\s*<|assistant:|\.\.\.|^\.{2,}|^#+\s|^\d+\.\s+\*\*",
        a,
        re.I,
    ):
        return True
    if a.count(":") >= 3 and not ("/" in a or a.startswith("~")):
        return True
    # truncated filesystem path (regex cap cut mid-segment) — forcing it verbatim
    # into VÝSLEDEK produces broken paths like `.../skills/devops/x/ref`
    if a.startswith(("/", "~")) and "/" in a:
        tail = a.rstrip("/").split("/")[-1]
        _ok_tails = {"md", "py", "sh", "js", "ts", "log", "json", "yaml", "yml", "txt", "db", "bak", "bin", "etc", "opt", "usr", "var", "tmp"}
        if tail.endswith((".", "-", "_")):
            return True
        if "." not in tail and tail.lower() not in _ok_tails:
            # extensionless tail: garbage if very short, or if the whole anchor
            # sits near the 60-char capture cap (regex cut it mid-segment)
            if len(tail) < 4 or len(a) >= 55:
                return True
    return False


def filter_scored_anchors(anchors: list[str]) -> list[str]:
    """Drop typo-pair noise and garbage tokens from anchor lists."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in anchors:
        a = raw.strip()
        if len(a) < 4 or is_garbage_anchor(a):
            continue
        if re.fullmatch(r"[a-záčďéěíňóřšťúůýž]+", a) and len(a) < 8:
            continue
        key = a.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out