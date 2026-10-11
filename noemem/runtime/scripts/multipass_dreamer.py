#!/usr/bin/env python3
"""Two-pass dreamer: extract facts, then write structured record."""
from __future__ import annotations

import re
from typing import Any, Callable

from dreamer_tiers import TierSpec, session_char_count, session_tier, tier_size_preamble
from prompt_variants import _prompt_anchors, strict_canonical_stems
from rubrics import parse_dreamer_output

ANCHOR_HIT_THRESHOLD = 0.8

EXTRACT_SYSTEM = (
    "You are a fact extractor for a session archive, edited for NEXT-MONTH use. "
    "The output is a machine-usable list — not a summary, no commentary, no timestamp field."
)

WRITE_SYSTEM = (
    "You are the EDITOR of a long-term memory archive. Your only test for every "
    "sentence: will it still be true and useful next month? End states, decisions, "
    "durable facts. Never the journey. Never paste markdown, XML, or meta-text like "
    "Wait/Let's. Write all values in English regardless of the session's language."
)

FIDELITY_RULES = """
Fidelity rules (violation = bad output):
- Use only whole commands, paths, keys and numbers — never copy broken fragments from the session.
- Forbidden: `:**`, `summary:`, `xml <string>`, a `bash` prefix if the session has no bash.
- If the session says a URL/IP/config was wrong or broken, state that explicitly.
- Include concrete numbers from the session (tok/s, GB, tensor/fix counts) if present.
- Every FAKTA line must be represented in vysledek or ponauceni (in backticks).
- Do not add facts that are not in FAKTA or the session. No meta-text.
- Editorial inclusion test: apply "will this still be true and useful next month?" to every fact. Install attempts, pip/venv versions, retries, timeouts, test-run timings, clone sizes, "server is up", approval-gate failures — all fail the test and must NOT appear, even as FAKTA lines. A failure is memory only if the failed approach is itself a durable lesson."""


def parse_fakta_output(text: str) -> list[str]:
    """Parse FAKTA lines or semicolon list from pass-1 extract."""
    text = (text or "").strip()
    if not text:
        return []
    facts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("FAKTA:"):
            rest = stripped[6:].strip()
            if rest:
                for part in re.split(r";\s*", rest):
                    p = part.strip()
                    if len(p) >= 4:
                        facts.append(p)
            continue
        m = re.match(r"^[-*]\s*(?:`([^`]+)`|(.+))$", stripped)
        if m:
            facts.append((m.group(1) or m.group(2) or "").strip())
            continue
        if stripped.startswith("- ") and ":" in stripped:
            facts.append(stripped[2:].strip())
    # Dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for f in facts:
        key = f.lower()
        if key not in seen and len(f) >= 4:
            seen.add(key)
            out.append(f)
    return out[:18]


def verify_injected_anchors(content: str, anchors: list[str]) -> list[str]:
    """Return anchors missing from dreamer output (case-insensitive substring)."""
    parsed = parse_dreamer_output(content)
    if not parsed:
        return list(anchors)
    blob = (
        (parsed.get("vysledek") or "")
        + " "
        + (parsed.get("ponauceni") or "")
        + " "
        + (parsed.get("proc") or "")
    ).lower()
    missing: list[str] = []
    for a in anchors:
        if a.lower() not in blob:
            missing.append(a)
    return missing


def build_extract_messages(
    fixture: dict,
    *,
    anchors: list[str] | None = None,
    tier: TierSpec | None = None,
    known_projects: list[str] | None = None,
) -> tuple[list[dict], dict]:
    _ = known_projects
    clean_text = fixture.get("clean_text", "")
    char_count = session_char_count(fixture)
    tier = tier or session_tier(char_count)
    anchors = anchors or _prompt_anchors(clean_text, n=tier.inject_anchor_count)
    anchor_hints = "\n".join(f"- `{a}`" for a in anchors) if anchors else "(none)"
    user = f"""Extract the 8–15 most important facts from this session for an archive record that will be read NEXT MONTH.

Each fact = a durable end state: a decision, a lasting artifact (path, service, config, cron), a measured capability, a deadline, or a plan change. Editorial test per fact: still true and useful next month? Install attempts, pip/venv versions, test-run timings, clone sizes, transient states — do NOT extract. Prefer these hints:
{anchor_hints}

Output format (only this, no other text):
FAKTA:
- `token or phrase` — one short sentence of context
- ...

Session ({char_count} characters):
{clean_text}"""
    return (
        [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": user},
        ],
        {"temperature": 0.1, "max_tokens": tier.extract_max_tokens},
    )


def build_write_messages(
    fixture: dict,
    fakta_text: str,
    *,
    anchors: list[str] | None = None,
    tier: TierSpec | None = None,
    missing_anchors: list[str] | None = None,
    known_projects: list[str] | None = None,
) -> tuple[list[dict], dict]:
    projects = known_projects if known_projects is not None else strict_canonical_stems()
    projekty_str = ", ".join(projects) if projects else "(žádné)"
    clean_text = fixture.get("clean_text", "")
    char_count = session_char_count(fixture)
    tier = tier or session_tier(char_count)
    session_start = fixture.get("session_start", "")[:19]
    anchors = anchors or _prompt_anchors(clean_text, n=tier.inject_anchor_count)
    anchor_block = ""
    if anchors:
        lines = "\n".join(f"- `{a}`" for a in anchors)
        anchor_block = f"""
Mandatory details (each must appear in vysledek or ponauceni, in backticks):
{lines}"""
    retry_block = ""
    if missing_anchors:
        miss = ", ".join(f"`{a}`" for a in missing_anchors[:8])
        retry_block = f"""
FIX: Your previous attempt was missing: {miss}.
Add them to vysledek without exceeding {tier.vysledek_vety_prompt}. Do not rewrite the other fields."""

    extra_preamble = (fixture.get("extra_preamble") or "").strip()
    extra_block = f"\n{extra_preamble}\n" if extra_preamble else ""

    user = f"""You are an EDITOR of a long-term memory archive. Create a structured record of the conversation between the user (the user) and the AI agent. The record will be read NEXT MONTH — for every sentence ask: will this still be true and useful then? WRITE IN ENGLISH regardless of the session's language; keep paths, commands and verbatim quotes as-is.

Session started: {session_start}
{tier_size_preamble(tier, char_count)}
{FIDELITY_RULES}
{anchor_block}
{retry_block}

Extracted facts from pass-1 (all must be represented in the output):
{fakta_text.strip()}

Mandatory fields:
- "timestamp": {session_start}
- "tema": short title (3–8 words)
- "proc": the request or problem
- "vysledek": ({tier.vysledek_vety_prompt})
- "ponauceni": ({tier.ponauceni_vety_prompt})
- "kategorie": personal | projects | maintenance | knowledge
- "projekt": canonical stem without -YYYYMMDD from: {projekty_str}. Assign by the MEANING of the work, not by a slug that merely appears in the text (model/inference → local-api, memory system → hermes-noemem, teaching → teacher). One-off work gets the closest existing project — never a new project name, never copy an example from this prompt.
- "supersedes": list of .md files (empty [] if none)

Format (respond with ONLY one valid JSON object, no other text before or after, values in English):
{{
  "timestamp": "{session_start}",
  "tema": "...",
  "proc": "...",
  "vysledek": "...",
  "ponauceni": "...",
  "kategorie": "...",
  "projekt": "...",
  "supersedes": []
}}
NO text outside the JSON. Do not copy internal tool IDs (TOOL[call_...], call_<hash>) into any value.
{extra_block}
Session:
{clean_text}"""
    return (
        [
            {"role": "system", "content": WRITE_SYSTEM},
            {"role": "user", "content": user},
        ],
        {"temperature": 0.15, "max_tokens": tier.write_max_tokens},
    )


def run_multipass_dreamer(
    fixture: dict,
    chat_fn: Callable[..., dict[str, Any]],
    api_model: str,
    *,
    known_projects: list[str] | None = None,
    thinking: bool = False,
    thinking_budget: int = 8192,
    max_tokens_thinking: int = 12288,
) -> dict[str, Any]:
    """Run extract → write → optional verify retry. chat_fn matches mlx sweep signature."""
    char_count = session_char_count(fixture)
    tier = session_tier(char_count)
    anchors = _prompt_anchors(fixture.get("clean_text", ""), n=tier.inject_anchor_count)

    chat_kw = dict(
        thinking=thinking,
        thinking_budget=thinking_budget,
        max_tokens_thinking=max_tokens_thinking,
    )

    p1_messages, p1_params = build_extract_messages(
        fixture, anchors=anchors, tier=tier, known_projects=known_projects
    )
    p1 = chat_fn(api_model, p1_messages, p1_params, **chat_kw)
    fakta_raw = p1.get("content") or ""
    fakta_list = parse_fakta_output(fakta_raw)

    p2_messages, p2_params = build_write_messages(
        fixture,
        fakta_raw if fakta_raw else "\n".join(f"- `{f}`" for f in fakta_list),
        anchors=anchors,
        tier=tier,
        known_projects=known_projects,
    )
    p2 = chat_fn(api_model, p2_messages, p2_params, **chat_kw)
    content = p2.get("content") or ""
    verify_retry = False
    missing_after: list[str] = []

    if anchors:
        missing = verify_injected_anchors(content, anchors)
        missing_after = missing
        if missing and len(missing) > len(anchors) * (1 - ANCHOR_HIT_THRESHOLD):
            verify_retry = True
            p2r_messages, p2r_params = build_write_messages(
                fixture,
                fakta_raw,
                anchors=anchors,
                tier=tier,
                missing_anchors=missing[:8],
                known_projects=known_projects,
            )
            p2_retry = chat_fn(api_model, p2r_messages, p2r_params, **chat_kw)
            if (p2_retry.get("content") or "").strip():
                content = p2_retry["content"]
                p2 = p2_retry
            missing_after = verify_injected_anchors(content, anchors)

    return {
        "content": content,
        "raw_content": p2.get("raw_content"),
        "reasoning": p2.get("reasoning"),
        "reasoning_chars": p2.get("reasoning_chars", 0),
        "salvage_note": p2.get("salvage_note"),
        "finish_reason": p2.get("finish_reason"),
        "completion_tokens": (p1.get("completion_tokens") or 0) + (p2.get("completion_tokens") or 0),
        "elapsed_s": round((p1.get("elapsed_s") or 0) + (p2.get("elapsed_s") or 0), 1),
        "tier": tier.label,
        "char_count": char_count,
        "multipass": True,
        "pass1_content": fakta_raw[:4000] if fakta_raw else None,
        "pass1_facts": fakta_list,
        "injected_anchors": anchors,
        "verify_retry": verify_retry,
        "missing_anchors_after": missing_after,
        "pass1_elapsed_s": p1.get("elapsed_s"),
        "pass2_elapsed_s": p2.get("elapsed_s"),
    }