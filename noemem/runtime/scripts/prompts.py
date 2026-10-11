#!/usr/bin/env python3
"""Exact prompt builders mirroring production scripts."""
from __future__ import annotations

import re
from pathlib import Path

_DATED_PROJECT_SUFFIX = re.compile(r"-\d{8}(-\d{8})?$")
_PROJEKTY_DIR = Path.home() / "Projekty"


def canonical_project_stems() -> list[str]:
    """Mirror dreamer_v2.load_prompt_projects()."""
    if not _PROJEKTY_DIR.exists():
        return []
    stems = sorted(f.stem for f in _PROJEKTY_DIR.glob("*.md"))
    canonical = {s for s in stems if not _DATED_PROJECT_SUFFIX.search(s)}
    out = set(canonical)
    for s in stems:
        if _DATED_PROJECT_SUFFIX.search(s):
            base = _DATED_PROJECT_SUFFIX.sub("", s)
            if base not in canonical:
                out.add(s)
    return sorted(out)


def dreamer_prompt(fixture: dict, known_projects: list[str] | None = None) -> tuple[list[dict], dict]:
    projekty_str = ", ".join(known_projects) if known_projects else "(žádné)"
    session_start = fixture.get("session_start", "")[:19]
    clean_text = fixture["clean_text"]
    user = f"""Jsi historický analytik. Každá session je záznam interakce mezi uživatelem (the user) a jeho AI agentem.

Tvým úkolem je vytvořit strukturovaný záznam o této konverzaci. Piš česky, srozumitelně, ale nešetři detaily — záznam by měl dávat smysl i za měsíc.

Session začala: {session_start}

Pro každou session zjisti:

- TIMESTAMP: použi session_start ({session_start})
- TÉMA: krátký název (3–8 slov), co se řešilo (použije se pro název souboru v archivu)
- PROČ: jaký byl požadavek nebo problém, co k tomu vedlo
- VÝSLEDEK: co se skutečně stalo — popiš průběh, ne jen "povedlo se". Pokud se něco nepovedlo, napiš to. Pokud se během konverzace měnil plán, zachyt to.
- PONAUČENÍ: co si z toho vzít. Ne "byla aktualizována dokumentace", ale JAKÝ pozorovatelný přínos to mělo nebo bude mít. Co by měl agent příště udělat jinak?
- KATEGORIE: personal | projects | maintenance | knowledge
- PROJEKT: kanonický název existujícího projektu (bez přípony -YYYYMMDD) z: {projekty_str}. Denní snapshoty v seznamu ignoruj — datum patří jen do TIMESTAMP, název archivního souboru řeší skript. Příklad: hermes-noemem, ne hermes-noemem-20260528. Pokud opravdu nic nesedí (nová opakující se práce bez domova), napiš `NOVÝ:` a krátký slug (malá písmena, číslice, pomlčky). Nikdy neopisuj příklad z tohoto zadání a nikdy nenavrhuj projekt pro jednorázovku — ta patří k nejbližšímu existujícímu projektu.
- SUPERSEDES: pokud tato session přímo nahrazuje starší záznam pro stejný PROJEKT, uveď filename(y). Jinak prázdné.

Formát:
TIMESTAMP: ...
TÉMA: ...
PROČ: ...
VÝSLEDEK: (odstavec, 2-5 vět)
PONAUČENÍ: (odstavec, 1-3 věty)
KATEGORIE: ...
PROJEKT: ...
SUPERSEDES: (nebo prázdné)

Session:
{clean_text}"""
    messages = [
        {
            "role": "system",
            "content": "Jsi historický analytik. Píšeš česky, strukturovaně, s dostatkem detailu.",
        },
        {"role": "user", "content": user},
    ]
    # Production uses 1000; reasoning-on needs headroom because MTPLX may emit
    # chain-of-thought inside content (not message.reasoning) for Qwen.
    params = {"temperature": 0.3, "max_tokens": 1000}
    return messages, params


def apply_reasoning_params(script: str, params: dict, mode: str) -> dict:
    out = dict(params)
    if mode == "on" and script == "dreamer":
        out["max_tokens"] = 4096
    return out


def nightly_prompt(fixture: dict) -> tuple[list[dict], dict]:
    goals = fixture.get("goals_text", "")
    sessions = fixture.get("session_summary", "")
    user = f"""Máš k dispozici aktuální goal strom a dnešní session.

=== AKTUÁLNÍ GOAL STROM ===
{goals}

=== DNEŠNÍ SESSION ===
{sessions}

Úkoly:
1. KE KTERÉMU CÍLI patří každá session? Označ [G1], [G2], [G3] nebo [NOVÝ]
2. JE NĚCO ZASTARALÉ? Změnila se priorita nebo status nějakého subtasku?
3. NOVÉ CÍLE? Objevil se pattern který neodpovídá žádnému existujícímu cíli?
4. Co by stálo za to propsat do _vztahovy-profil.md? (změna dynamiky, nový princip)

Odpověz STRUKTUROVANĚ:
### Session → Cíle
(session krátce → Gx)

### Změny v cílech
(pokud něco)

### Návrhy ke schválení
(co bych měl změnit, schválit)
"""
    messages = [
        {"role": "system", "content": "Odpovídej přímo a strukturovaně. Piš česky."},
        {"role": "user", "content": user},
    ]
    params = {"temperature": 0.2, "max_tokens": 8192}
    return messages, params


def reasoning_extra(script: str, mode: str) -> dict:
    """Match production scripts when off; explicit enable when on."""
    if mode == "off":
        if script == "dreamer":
            return {"reasoning": {"enabled": False}}
        return {
            "reasoning": {"enabled": False},
            "reasoning_budget_tokens": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    # reasoning on
    return {
        "reasoning": {"enabled": True},
        "chat_template_kwargs": {"enable_thinking": True},
    }