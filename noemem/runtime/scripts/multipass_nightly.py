#!/usr/bin/env python3
"""Two-pass nightly reflection: extract insights, then write project-centric _goals-agent."""
from __future__ import annotations

import re
from typing import Any, Callable

from dreamer_tiers import session_tier

# Token budgets from runtime/model_config.json (roles nightly_extract / nightly_write).
# Fallback to generous defaults if config missing.
try:
    from noemem_paths import role_config
    _EXT_CFG = role_config("nightly_extract") or {}
    _WRITE_CFG = role_config("nightly_write") or {}
except Exception:
    _EXT_CFG = {}
    _WRITE_CFG = {}

EXTRACT_MAX_TOKENS = int(_EXT_CFG.get("max_tokens", 4096)) if _EXT_CFG else 4096
WRITE_MAX_TOKENS = int(_WRITE_CFG.get("max_tokens", 8192)) if _WRITE_CFG else 8192

# Celý _goals.md uživatele — u ~10k znaků neřezat (dříve [:3000] zbytečně skrýval Teacher Progress).
GOALS_MAX_CHARS = 20_000

EXTRACT_SYSTEM = (
    "Jsi analytik _goals.md (uživatel) a Dreamer archivů. Výstup je strojově zpracovatelný seznam FAKTA — "
    "ne deník, žádné ### sekce. Nepoužívej [G1]–[G9]; mapuj na slug projektu nebo název sekce z _goals.md."
)

WRITE_SYSTEM_DAILY = (
    "Jsi editor _goals-agent.md (operativní poznámky agenta). Piš česky, stručně. "
    "Nevymýšlej strategické pilíře. Údržba/cron/fix = jedna věta pod archiv, ne nový projekt. "
    "Progress v _goals.md (hotové předměty, dokončené fáze) má přednost před starými dohady z archivů."
)

WRITE_SYSTEM_WEEKLY = (
    "Jsi editor _goals-agent.md (týdenní rozvaha). Piš česky. _goals.md = autorita; "
    "archivy = co se stalo. Navrhuj nové projekty jen s oporou v archivu a označ [NÁVRH — ke schválení]."
)

DAILY_SECTION_MARKERS = (
    "### Mapování archivů → projekty",
    "### Stav projektů",
    "### Agent potřebuje",
    "### Návrhy nových projektů",
    "### Navrhované úpravy _goals.md",
)

WEEKLY_SECTION_MARKERS = DAILY_SECTION_MARKERS + ("### Vztah / spolupráce (týden)",)

PROJECT_SLUG_RE = re.compile(
    r"\*\*[a-z0-9][a-z0-9_-]*\*\*|→\s*\*\*[a-z0-9][a-z0-9_-]*\*\*|bez\s+projektu",
    re.I,
)


def _prepare_goals_text(goals: str) -> str:
    g = (goals or "").strip()
    if len(g) <= GOALS_MAX_CHARS:
        return g
    head = g[: GOALS_MAX_CHARS - 120]
    return head + "\n\n[… _goals.md zkráceno — zbytek na disku; respektuj Progress u Teacher a Projects …]\n"


def _prepare_sessions_text(sessions: str, mode: str, tier) -> str:
    cap = 12_000 if mode == "weekly" else 8_000
    s = sessions or ""
    if len(s) <= cap:
        return s
    return s[: cap - 80] + "\n\n[… archivy zkráceny …]\n"


def _content_size(goals: str, sessions: str) -> int:
    return len(goals or "") + len(sessions or "")


def build_nightly_extract_messages(
    goals: str,
    sessions: str,
    *,
    mode: str = "daily",
    catalog: str = "",
) -> tuple[list[dict], dict]:
    char_count = _content_size(goals, sessions)
    tier = session_tier(char_count)
    mode_label = "denní (24h)" if mode == "daily" else "týdenní (7d)"
    goals_block = _prepare_goals_text(goals)
    sessions_block = _prepare_sessions_text(sessions, mode, tier)
    catalog_block = (catalog or "").strip()[:4000]

    user = f"""Z _goals.md (uživatel = pravda) a Dreamer archivů připrav FAKTA pro {mode_label} přehled do _goals-agent.md.

Pravidla:
- Archivy = výstupy Dreameru (TÉMA, VÝSLEDEK, pole project). Nevymýšlej archivy, které ve vstupu nejsou.
- Mapuj každý archiv na **slug projektu** z katalogu nebo na sekci v _goals.md (Teacher, Hermes-noemem, …). Žádné [G1]–[G9].
- Pokud _goals.md říká že něco je hotové (např. předmět complete), nesmíš v faktech psát že „pokračuje“.
- project z frontmatter archivu použij jako první hint; slug normalizuj (malá písmena, pomlčky).
- Údržba / jednorázový fix: jedna věta dopadu, ne nový strategický projekt.
- Nový projekt jen pokud archiv ukazuje opakovanou práci mimo katalog → označ slug jako navrh-nazev [NÁVRH].
- Všechny session ve vstupu jsou relevantní — nefiltruj „důležitost“.

Formát výstupu (pouze toto):
FAKTA:
- archiv: `soubor.md` → projekt: **slug** nebo bez projektu — jedna věta dopadu
- stav_projektu: **slug** — max jedna věta; jen projekty z ## Active now nebo zmíněné v archivu; nekopíruj celý Progress
- agent_potrebuje: bullet (co agent potřebuje vědět pro spolupráci) nebo žádné
- navrh_novy_projekt: **slug** [NÁVRH] — důvod (nebo žádné)
- návrhy_goals: konkrétní diff návrhy pro _goals.md (nebo žádné)
- profil: vztahové z týdne (nebo žádné)

=== KATALOG PROJEKTŮ (slugy) ===
{catalog_block or "(žádný katalog — použij project z archivu a sekce _goals.md)"}

=== _goals.md (uživatel, celý nebo max {GOALS_MAX_CHARS} znaků) ===
{goals_block}

=== DREAMER ARCHIVY ===
{sessions_block}
"""
    max_tok = tier.extract_max_tokens if mode == "weekly" else min(EXTRACT_MAX_TOKENS, tier.extract_max_tokens)
    return (
        [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": user},
        ],
        {"temperature": 0.1, "max_tokens": max_tok},
    )


def build_nightly_write_messages(
    goals: str,
    sessions: str,
    fakta_text: str,
    *,
    mode: str = "daily",
    catalog: str = "",
    missing_sections: list[str] | None = None,
) -> tuple[list[dict], dict]:
    char_count = _content_size(goals, sessions)
    tier = session_tier(char_count)
    retry = ""
    if missing_sections:
        retry = (
            f"\nOPRAVA: V předchozím pokusu chyběly sekce: {', '.join(missing_sections)}. "
            "Doplň je — nepřepisuj zbytek zbytečně.\n"
        )

    structure = """
Napiš tělo souboru _goals-agent.md (bez YAML frontmatter — doplní skript). Žádný deník, žádné [G1]–[G9].

### Mapování archivů → projekty
(jedna řádka na každý archiv ze vstupu: `soubor.md` → **slug** — věta; nebo → bez projektu)

### Stav projektů
(max 6 bullets; slug z katalogu; jen Active now + projekty z archivů; detail předmětů → viz _goals.md Progress, neopakovat seznam)

### Agent potřebuje
(co agent potřebuje pro další práci se mnou; ne strategické slogany)

### Návrhy nových projektů
(pouze [NÁVRH — ke schválení]; pokud nic: „Žádné.“)

### Navrhované úpravy _goals.md
(konkrétní návrhy ke schválení; pokud nic: „Žádné návrhy.“)
"""
    if mode == "weekly":
        structure += """
### Vztah / spolupráce (týden)
(pouze pokud FAKTA obsahují profil; jinak „Žádné.“)
"""
        system = WRITE_SYSTEM_WEEKLY
    else:
        system = WRITE_SYSTEM_DAILY

    goals_block = _prepare_goals_text(goals)
    sessions_block = _prepare_sessions_text(sessions, mode, tier)

    user = f"""{retry}Z FAKTA sestav _goals-agent.md ({mode}).

_goals.md je autorita — nesmíš tvrdit opak Progress sekci (hotové předměty, dokončené mise).
{structure}

=== FAKTA (pass 1) ===
{fakta_text.strip()}

=== _goals.md ===
{goals_block}

=== DREAMER ARCHIVY ===
{sessions_block}
"""
    return (
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        {"temperature": 0.15, "max_tokens": WRITE_MAX_TOKENS},
    )


def verify_nightly_sections(content: str, *, mode: str = "daily") -> list[str]:
    """Return missing section markers."""
    markers = WEEKLY_SECTION_MARKERS if mode == "weekly" else DAILY_SECTION_MARKERS
    missing = [m for m in markers if m not in content]
    if "### Mapování archivů" in content or "### Mapování archivů → projekty" in content:
        if not PROJECT_SLUG_RE.search(content) and "bez projektu" not in content.lower():
            if "žádn" not in content.lower() and "`" not in content:
                missing.append("[mapování: slug nebo bez projektu]")
    return missing


def run_multipass_nightly(
    goals: str,
    sessions: str,
    chat_fn: Callable[..., dict[str, Any]],
    api_model: str,
    *,
    mode: str = "daily",
    catalog: str = "",
) -> dict[str, Any]:
    """Extract → write → optional section verify retry."""
    p1_msg, p1_params = build_nightly_extract_messages(
        goals, sessions, mode=mode, catalog=catalog
    )
    p1 = chat_fn(api_model, p1_msg, p1_params)
    fakta = (p1.get("content") or "").strip()

    p2_msg, p2_params = build_nightly_write_messages(
        goals, sessions, fakta, mode=mode, catalog=catalog
    )
    p2 = chat_fn(api_model, p2_msg, p2_params)
    content = (p2.get("content") or "").strip()
    verify_retry = False
    missing: list[str] = []

    miss = verify_nightly_sections(content, mode=mode)
    if miss:
        verify_retry = True
        missing = miss
        p2r_msg, p2r_params = build_nightly_write_messages(
            goals,
            sessions,
            fakta,
            mode=mode,
            catalog=catalog,
            missing_sections=miss[:5],
        )
        p2_retry = chat_fn(api_model, p2r_msg, p2r_params)
        if (p2_retry.get("content") or "").strip():
            content = p2_retry["content"].strip()
            p2 = p2_retry
        missing = verify_nightly_sections(content, mode=mode)

    return {
        "content": content,
        "mode": mode,
        "multipass": True,
        "pass1_content": fakta[:4000] if fakta else None,
        "verify_retry": verify_retry,
        "missing_sections_after": missing,
        "elapsed_s": round((p1.get("elapsed_s") or 0) + (p2.get("elapsed_s") or 0), 1),
        "completion_tokens": (p1.get("completion_tokens") or 0) + (p2.get("completion_tokens") or 0),
    }