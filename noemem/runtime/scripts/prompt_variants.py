#!/usr/bin/env python3
"""Dreamer prompt variants for MoE quality experiments."""
from __future__ import annotations

import re

from dreamer_tiers import session_char_count, session_tier, tier_size_preamble
from dreamer_tiers import is_garbage_anchor as _is_garbage_anchor
from prompts import canonical_project_stems
from rubrics import _session_anchor_tokens


def strict_canonical_stems() -> list[str]:
    """Stems without any -YYYYMMDD fragments (avoids polluted snapshot chains)."""
    return sorted(s for s in canonical_project_stems() if not re.search(r"-\d{8}", s))


def _prompt_anchors(session_text: str, n: int = 8) -> list[str]:
    """Pick high-signal session anchors for injection into the user prompt."""
    scored: list[tuple[float, str]] = []
    for anchor in _session_anchor_tokens(session_text):
        a = anchor.strip()
        if len(a) < 4 or _is_garbage_anchor(a):
            continue
        if re.fullmatch(r"[a-záčďéěíňóřšťúůýž]+", a) and len(a) < 8:
            continue
        score = 0.0
        if "/" in a or a.startswith("~"):
            score += 3.0
        if "." in a and not a[0].isdigit():
            score += 2.0
        if any(x in a for x in ("mtplx", "forge", "optiq", "launchctl", "brew", "--", ".py", ".sh")):
            score += 2.0
        score += min(len(a), 50) / 25.0
        scored.append((score, a))
    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[str] = []
    seen: set[str] = set()
    for _, a in scored:
        if a in seen:
            continue
        seen.add(a)
        out.append(a)
        if len(out) >= n:
            break
    return out


def _base_fields(projekty_str: str, session_start: str) -> str:
    return f"""Session začala: {session_start}

Povinná pole:

- TIMESTAMP: použi session_start ({session_start})
- TÉMA: krátký název (3–8 slov)
- PROČ: požadavek nebo problém, co k tomu vedlo
- VÝSLEDEK: viz pravidla níže
- PONAUČENÍ: viz pravidla níže
- KATEGORIE: personal | projects | maintenance | knowledge
- PROJEKT: kanonický název (bez -YYYYMMDD) z: {projekty_str}. Příklad: hermes-noemem. Jinak `NOVÝ:` + krátký slug (jednorázovka nikdy — patří k nejbližšímu existujícímu projektu).
- SUPERSEDES: filename(y) nebo prázdné

Formát výstupu (přesně takto, žádný jiný text):
TIMESTAMP: ...
TÉMA: ...
PROČ: ...
VÝSLEDEK: ...
PONAUČENÍ: ...
KATEGORIE: ...
PROJEKT: ...
SUPERSEDES:"""


VARIANTS: dict[str, dict] = {
    "baseline": {
        "desc": "Current production prompt",
        "system": "Jsi historický analytik. Píšeš česky, strukturovaně, s dostatkem detailu.",
        "extra_user": "",
        "temperature": 0.3,
        "max_tokens": 1000,
        "use_legacy": True,
    },
    "strict_length": {
        "desc": "Hard sentence caps on VÝSLEDEK/PONAUČENÍ",
        "system": "Jsi historický analytik. Píšeš česky. Striktně dodržíš počet vět — ani víc, ani míň.",
        "extra_user": """
PRAVIDLA DÉLKY (povinné):
- VÝSLEDEK: přesně 3 nebo 4 věty. Jedna věta = jedna myšlenka, končí tečkou.
- PONAUČENÍ: přesně 2 věty.
- Nepiš úvod, závěr ani odrážky mimo pole.""",
        "temperature": 0.3,
        "max_tokens": 2000,
    },
    "concrete_facts": {
        "desc": "Each VÝSLEDEK sentence must cite a concrete fact from session",
        "system": "Jsi historický analytik. Píšeš česky. Každá věta obsahuje ověřitelný fakt ze session.",
        "extra_user": """
PRAVIDLA KONKRÉTNOSTI (povinné):
- VÝSLEDEK: 3–4 věty. V KAŽDÉ větě uveď alespoň jeden konkrétní fakt ze session: název souboru, cesta, příkaz, číslo, config klíč, název skriptu nebo nástroje.
- PONAUČENÍ: 2 věty s actionable doporučením pro agenta příště.
- Zakázáno: vágní fráze bez faktů ("byla aktualizována dokumentace", "povedlo se", "proběhla analýza").""",
        "temperature": 0.3,
        "max_tokens": 2000,
    },
    "strict_concrete": {
        "desc": "Strict length + concrete facts combined",
        "system": "Jsi historický analytik. Píšeš česky, stručně a fakticky. Dodržíš počet vět a konkrétní fakta.",
        "extra_user": """
PRAVIDLA (povinné):
- VÝSLEDEK: přesně 3 nebo 4 věty. Každá věta = jeden konkrétní fakt ze session (soubor, cesta, příkaz, číslo, klíč v configu).
- PONAUČENÍ: přesně 2 věty — co agent příště udělá jinak, s odkazem na postup.
- Žádné obecné shrnutí. Žádné odrážky. Žádný text mimo pole.""",
        "temperature": 0.2,
        "max_tokens": 2000,
    },
    "few_shot": {
        "desc": "Strict concrete + inline example",
        "system": "Jsi historický analytik. Píšeš česky. Napodobíš styl příkladu níže.",
        "extra_user": """
PRAVIDLA: VÝSLEDEK 3–4 věty s konkrétními fakty. PONAUČENÍ 2 věty.

Příklad dobrého výstupu:
TIMESTAMP: 2026-05-28T14:50:28
TÉMA: Audit paměti hermes-noemem
PROČ: the user chtěl ověřit kvalitu výstupů Dreameru a chybějící session.
VÝSLEDEK: Nalezeno write_json_snapshots: false v konfiguraci Hermes. Devět session nemělo content soubory, data byla obnovena ze state.db (748 MB). Dreamer měl vypnutý enable_thinking kvůli template retry. QMD index obsahoval 64 dokumentů po obnově.
PONAUČENÍ: Před závěrem o ztrátě dat ověř config write_json_snapshots a fallback ze state.db. U extrakčních úloh nech enable_thinking vypnutý.
KATEGORIE: maintenance
PROJEKT: hermes-noemem
SUPERSEDES:""",
        "temperature": 0.2,
        "max_tokens": 2000,
    },
    "low_temp_tight": {
        "desc": "Very low temperature + tight system",
        "system": "Jsi přesný archivář session. Výstup jen ve formátu polí. VÝSLEDEK max 4 věty, každá s faktem. PONAUČENÍ 2 věty.",
        "extra_user": """
VÝSLEDEK: 3–4 věty, konkrétní fakty ze session (soubory, příkazy, čísla).
PONAUČENÍ: 2 věty, actionable.""",
        "temperature": 0.1,
        "max_tokens": 2000,
    },
    "hybrid_mild": {
        "desc": "Production-style prompt + mild fact density instruction",
        "system": "Jsi historický analytik. Píšeš česky, strukturovaně. VÝSLEDEK je stručný ale faktický.",
        "extra_user": """
Doplňující pravidla:
- VÝSLEDEK: 3–4 věty (ne víc). Každá věta alespoň jeden konkrétní detail ze session.
- PONAUČENÍ: 2 věty, co agent udělá jinak příště.
- PROJEKT: vždy kanonický stem bez data (hermes-noemem, xnotes, teacher, mtplx — nikdy hermes-noemem-20260528).""",
        "temperature": 0.3,
        "max_tokens": 2000,
        "use_production_user": True,
    },
    "anchor_backticks": {
        "desc": "Require backtick-quoted paths/commands from session in VÝSLEDEK",
        "system": "Jsi historický analytik. Výstup je archivní záznam — fakta v backticích podle session.",
        "extra_user": """
Doplňující pravidla:
- VÝSLEDEK: 3–4 věty. V každé větě alespoň jeden výraz v `backticks` převzatý ze session (soubor, cesta, příkaz, config klíč).
- PONAUČENÍ: 2 věty.
- PROJEKT: kanonický stem bez -YYYYMMDD.""",
        "temperature": 0.25,
        "max_tokens": 2000,
        "use_production_user": True,
    },
    "extract_then_write": {
        "desc": "Explicit 4-fact checklist before fields",
        "system": "Jsi historický analytik. Nejdřív vybereš fakta, pak vyplníš pole. Žádný jiný text.",
        "extra_user": """
Postup (dodrž pořadí):
1) Na začátku odpovědi uveď řádek: FAKTA: fakt1; fakt2; fakt3; fakt4 — každý fakt = konkrétní detail ze session.
2) Pak vyplň standardní pole TIMESTAMP až SUPERSEDES.
- VÝSLEDEK musí obsahovat všechny 4 fakty (přeformulované do vět).
- VÝSLEDEK: 3–4 věty. PONAUČENÍ: 2 věty.""",
        "temperature": 0.2,
        "max_tokens": 2500,
        "use_production_user": True,
    },
    "anti_verbosity": {
        "desc": "Production prompt + hard anti-verbosity + anchor density",
        "system": "Jsi archivář session. Stručný, faktický výstup. Nikdy nepřekročíš počet vět.",
        "extra_user": """
Striktní limity (porušení = špatný výstup):
- VÝSLEDEK: přesně 3 nebo 4 věty. Každá věta obsahuje konkrétní detail ze session (soubor, cesta, příkaz, číslo, config klíč) — ideálně v `backticks`.
- PONAUČENÍ: přesně 2 věty.
- PROJEKT: vždy řádek začínající PROJEKT: (ne PROJECT:), kanonický stem bez -YYYYMMDD.""",
        "temperature": 0.2,
        "max_tokens": 2000,
        "use_production_user": True,
        "vysledek_vety": "přesně 3 nebo 4 věty",
        "ponauceni_vety": "přesně 2 věty",
    },
    "anchor_inject": {
        "desc": "Inject top session anchors; each must appear in VÝSLEDEK",
        "system": "Jsi archivář session. VÝSLEDEK je faktický — použiješ dodané detaily ze session.",
        "extra_user": "",  # filled dynamically
        "temperature": 0.2,
        "max_tokens": 2500,
        "use_production_user": True,
        "inject_anchors": True,
    },
    "anti_verbosity_anchors": {
        "desc": "anti_verbosity + injected session anchors",
        "system": "Jsi archivář session. Stručný, faktický výstup. Nikdy nepřekročíš počet vět.",
        "extra_user": """
Striktní limity:
- VÝSLEDEK: přesně 3 nebo 4 věty. Každá věta alespoň jeden povinný detail níže (v `backticks`).
- PONAUČENÍ: přesně 2 věty.
- PROJEKT: řádek PROJEKT: (ne PROJECT:), kanonický stem bez -YYYYMMDD.""",
        "temperature": 0.2,
        "max_tokens": 2500,
        "use_production_user": True,
        "vysledek_vety": "přesně 3 nebo 4 věty",
        "ponauceni_vety": "přesně 2 věty",
        "inject_anchors": True,
    },
    "anchor_fidelity": {
        "desc": "Anchor inject + anti-garble, wrong-fact, compression guards",
        "system": (
            "Jsi archivář session. VÝSLEDEK je faktický výčet ověřitelných detailů ze session — "
            "ne literární shrnutí. Nikdy nevkládáš střihy markdownu, XML ani neúplné věty ze session."
        ),
        "extra_user": """
Striktní limity:
- VÝSLEDEK: přesně 3 nebo 4 věty. Každá věta alespoň jeden povinný detail níže (v `backticks`).
- PONAUČENÍ: přesně 2 věty.
- PROJEKT: řádek PROJEKT: (ne PROJECT:), kanonický stem bez -YYYYMMDD.

Pravidla věrnosti (porušení = špatný výstup):
- Používej jen celé příkazy, cesty, klíče a čísla — nikdy nekopíruj rozbité střihy ze session.
- Zakázáno vkládat: `:**`, `shrnutí:`, `xml <string>`, neúplné markdown bloky, prefix `bash` pokud session nemá bash.
- Každý `backtick` výraz musí být celý a významový (soubor, cesta, příkaz, config klíč, metrika).
- Pokud session říká, že URL/IP/config byl špatný nebo nefunkční, napiš to explicitně — ne „běží správně“.
- Uveď konkrétní čísla ze session (tok/s, GB, počet tensorů/oprav, verze) pokud existují.
- Zakázáno: vágní věty bez pojmenovaného artefaktu („byla provedena analýza“, „pipeline vytvořen"").
- Povinné detaily níže přepiš do správných vět; pokud je anchor fragment, obnov ho do celého příkazu nebo cesty.""",
        "temperature": 0.15,
        "max_tokens": 2500,
        "use_production_user": True,
        "vysledek_vety": "přesně 3 nebo 4 věty",
        "ponauceni_vety": "přesně 2 věty",
        "inject_anchors": True,
        "inject_anchor_count": 12,
    },
    "tiered_anchor_fidelity": {
        "desc": "anchor_fidelity + size-aware sentence limits and anchor count",
        "system": (
            "You are the EDITOR of a long-term memory archive. Your only test for "
            "every sentence: will it still be true and useful next month? End states, "
            "decisions, durable facts. Never the journey. Nikdy nevkládáš střihy "
            "markdownu, XML ani neúplné věty ze session."
        ),
        "extra_user": """
Pravidla věrnosti (porušení = špatný výstup):
- Používej jen celé příkazy, cesty, klíče a čísla — nikdy nekopíruj rozbité střihy ze session.
- Zakázáno vkládat: `:**`, `shrnutí:`, `xml <string>`, neúplné markdown bloky, prefix `bash` pokud session nemá bash.
- Každý `backtick` výraz musí být celý a významový (soubor, cesta, příkaz, config klíč, metrika).
- Pokud session říká, že URL/IP/config byl špatný nebo nefunkční, napiš to explicitně — ne „běží správně“.
- Uveď konkrétní čísla ze session (tok/s, GB, počet tensorů/oprav, verze) pokud existují.
- Zakázáno: vágní věty bez pojmenovaného artefaktu („byla provedena analýza“, „pipeline vytvořen"").
- Povinné detaily níže přepiš do správných vět; pokud je anchor fragment, obnov ho do celého příkazu nebo cesty.
- Projektové interní zkratky a čísla („Fix 4", „G2", číslo PR, „revert 8a3f") nevysvětlené v textu session: pokud je z kontextu poznat, oč jde, rozepiš to jedním srozumitelným slovním popisem (např. „Fix 4 = oprava embedding modelu a re-embed 25 memories") místo holé zkratky. Pokud z kontextu poznat nelze, uveď zkratku a připiš, že šlo o interní označení bez bližšího detailu v session — nikdy zkratku nevymýšlej ani nefabuluj, co znamená.
- Pravidla LITERÁLNÍ INTERPRETACE (klíčové, porušení = špatný výstup):
  - Editorial inclusion test (klíčové): na každý fakt aplikuj „bude to pravdivé a užitečné i za měsíc?“. Instalační pokusy, pip/venv verze, retry, timeouty, timingy testovacích běhů, velikosti klonů, „server běží“, selhání approval-gate — všechno testem propadá a do VÝSLEDEK/FAKTA nepatří, i když je to fakticky správné. Selhání je pamětí jen tehdy, když samo je trvalou lekcí.
  - TÉMA/PROČ/VÝSLEDEK/PROJEKT popisují KONKRÉTNÍ práci, kterou session doopravdy řešila (nástroj, fix, konfigurace, test) — ne abstraktní obecný dojem z výstupu.
  - Zakázáno přeformulovat konkrétní práci do vágní/agentní fráze (např. „výstupy jsou příliš plynulé", „zachycování hranic", „mechanismy vynucující rozdělení", „testování limitů generování"). Pokud session testuje konkrétní fix (např. TTS long-reply cutoff), řekni to jeho jménem: „test opravy cutoffu hlasových odpovědí".
  - PROJEKT odvoď z KONKRÉTNÍHO předmětu práce (hlas/STT/TTS → voice-stt-tts; paměť → hermes-noemem; model/inference → local-api), ne z abstraktního dojmu z výstupu. „NOVÝ" jen pro novou opakovanou práci bez existujícího domova.
  - Přímou výpověď uživatele (jeho doslovná slova o problému, např. „you did not pick my previous sentence") raději cituj/parafrázuj přesně, než abys ji nahradil abstraktním popisem.
""" ,
        "temperature": 0.15,
        "max_tokens": 3000,
        "use_production_user": True,
        "inject_anchors": True,
        "use_tier": True,
    },
    "multipass_anchor_fidelity": {
        "desc": "Two-pass extract→write with tier-aware limits and verify retry",
        "multipass": True,
        "temperature": 0.15,
        "max_tokens": 3000,
        "use_production_user": False,
        "inject_anchors": True,
        "use_tier": True,
    },
}


def _production_user_prompt(
    fixture: dict,
    known_projects: list[str],
    extra: str = "",
    *,
    vysledek_vety: str = "3–4 věty",
    ponauceni_vety: str = "2 věty",
) -> str:
    """Mirror dreamer_v2.py / prompts.py production user block."""
    projekty_str = ", ".join(known_projects) if known_projects else "(žádné)"
    session_start = fixture.get("session_start", "")[:19]
    clean_text = fixture["clean_text"]
    tail = f"\n{extra.strip()}\n" if extra.strip() else "\n"
    return f"""Jsi EDITOR dlouhodobého paměťového archivu. Každá session je záznam interakce mezi uživatelem (the user) a jeho AI agentem. Archiv se bude číst ZA MĚSÍC — na každou větu se ptáš: bude to pak stále pravdivé a užitečné?

Tvým úkolem je vytvořit strukturovaný záznam o této konverzaci. Piš anglicky, srozumitelně; vlastní jména, cesty, příkazy a doslovné české citace nechávej v originále.

Session začala: {session_start}

Pro každou session zjisti:

- TIMESTAMP: použi session_start ({session_start})
- TÉMA: krátký název (3–8 slov), co se řešilo (použije se pro název souboru v archivu)
- PROČ: jaký byl požadavek nebo problém, co k tomu vedlo
- VÝSLEDEK: POUZE koncový stav — rozhodnutí, změny stavu projektu, trvalé fakty (cesty k trvalým artefaktům, služby, configy, crony, změřené schopnosti, deadliny). Každou větu testuj: bude pravdivá a užitečná i za měsíc? Pokud ne, vymaž ji. NEvyprávěj průběh: žádné pokusy, retry, selhání nástrojů, instalační kroky, výpisy příkazů, timingy testovacích běhů. Výjimka: selhání je pamětí jen tehdy, když samo o sobě je trvalou lekcí („nástroj X neumí Y — použij Z“).
- PONAUČENÍ: přenositelná lekce, pokud existuje; pokud ne, napiš „none“. Ne „byla aktualizována dokumentace“.
- KATEGORIE: personal | projects | maintenance | knowledge
- PROJEKT: kanonický název existujícího projektu (bez přípony -YYYYMMDD) z: {projekty_str}. Denní snapshoty v seznamu ignoruj — datum patří jen do TIMESTAMP, název archivního souboru řeší skript. Příklad: hermes-noemem, ne hermes-noemem-20260528. Pokud opravdu nic nesedí (nová opakující se práce bez domova), napiš `NOVÝ:` a krátký slug (malá písmena, číslice, pomlčky). Nikdy neopisuj příklad z tohoto zadání a nikdy nenavrhuj projekt pro jednorázovku — ta patří k nejbližšímu existujícímu projektu.
- SUPERSEDES: pokud tato session přímo nahrazuje starší záznam pro stejný PROJEKT, uveď filename(y). Jinak prázdné.
{tail}
Formát (přesně tato pole, žádný jiný text — včetně řádku PROJEKT:, ne PROJECT:):
TIMESTAMP: ...
TÉMA: ...
PROČ: ...
VÝSLEDEK: ({vysledek_vety})
PONAUČENÍ: ({ponauceni_vety})
KATEGORIE: ...
PROJEKT: ...
SUPERSEDES: (nebo prázdné)

Session:
{clean_text}"""


def _tier_overrides(fixture: dict, spec: dict) -> dict:
    """Apply size-tier limits to variant spec fields."""
    tier = session_tier(session_char_count(fixture))
    out = dict(spec)
    out["vysledek_vety"] = tier.vysledek_vety_prompt
    out["ponauceni_vety"] = tier.ponauceni_vety_prompt
    out["inject_anchor_count"] = tier.inject_anchor_count
    out["max_tokens"] = max(int(spec.get("max_tokens", 2000)), tier.write_max_tokens)
    out["_tier"] = tier
    out["_char_count"] = session_char_count(fixture)
    return out


def dreamer_prompt_variant(
    fixture: dict,
    variant_id: str,
    known_projects: list[str] | None = None,
) -> tuple[list[dict], dict]:
    if variant_id not in VARIANTS:
        raise KeyError(f"Unknown variant: {variant_id}")

    spec = VARIANTS[variant_id]
    if spec.get("multipass"):
        raise ValueError(f"Variant {variant_id} requires multipass runner, not single-shot prompt")
    if spec.get("use_legacy"):
        from prompts import dreamer_prompt

        return dreamer_prompt(fixture, known_projects)

    projects = known_projects if known_projects is not None else strict_canonical_stems()
    if spec.get("use_production_user"):
        active = _tier_overrides(fixture, spec) if spec.get("use_tier") else spec
        extra = active.get("extra_user", "")
        if active.get("use_tier"):
            tier = active["_tier"]
            extra = (extra.strip() + tier_size_preamble(tier, active["_char_count"])).strip()
            extra = f"""
Striktní limity:
- VÝSLEDEK: {tier.vysledek_vety_prompt}. Každá věta alespoň jeden povinný detail níže (v `backticks`).
- PONAUČENÍ: {tier.ponauceni_vety_prompt}.
- PROJEKT: řádek PROJEKT: (ne PROJECT:), kanonický stem bez -YYYYMMDD.
- PRAVIDLO PRO CESTY (povinné): každou cestu k souboru přepisuj VERBATIM ze session — přesná velikost písmen, jak je na disku. Cesta, která na disku neexistuje, selže v lintu a záznam skončí v karanténě. Žádné zkrácení `...` (celá cesta nebo nic); pokud si přesnou cestou nejsi jistý, popiš soubor slovně.

{extra}"""
        anchors: list[str] = []
        if active.get("inject_anchors"):
            n_anchors = int(active.get("inject_anchor_count", 8))
            anchors = _prompt_anchors(fixture.get("clean_text", ""), n=n_anchors)
            if anchors:
                lines = "\n".join(f"- `{a}`" for a in anchors)
                inject = f"""
Povinné detaily ze session (každý musí být ve VÝSLEDEK, v backticks — přepiš rozbité fragmenty do celých příkazů/cest):
{lines}"""
                extra = (extra.strip() + inject).strip()
        user = _production_user_prompt(
            fixture,
            projects,
            extra,
            vysledek_vety=active.get("vysledek_vety", "3–4 věty"),
            ponauceni_vety=active.get("ponauceni_vety", "2 věty"),
        )
        messages = [
            {"role": "system", "content": active.get("system", spec["system"])},
            {"role": "user", "content": user},
        ]
        params = {
            "temperature": active.get("temperature", spec.get("temperature", 0.3)),
            "max_tokens": active.get("max_tokens", spec.get("max_tokens", 2000)),
        }
        if active.get("use_tier"):
            params["_tier"] = active["_tier"].label
            params["_injected_anchors"] = anchors
        return messages, params

    projects = known_projects if known_projects is not None else strict_canonical_stems()
    projekty_str = ", ".join(projects) if projects else "(žádné)"
    session_start = fixture.get("session_start", "")[:19]
    clean_text = fixture["clean_text"]
    fields = _base_fields(projekty_str, session_start)
    extra = spec.get("extra_user", "").strip()

    user = f"""Jsi historický analytik. Vytvoř strukturovaný záznam o konverzaci mezi uživatelem (the user) a AI agentem.

{fields}
{chr(10) + extra if extra else ""}

Session:
{clean_text}"""

    messages = [
        {"role": "system", "content": spec["system"]},
        {"role": "user", "content": user},
    ]
    params = {
        "temperature": spec.get("temperature", 0.3),
        "max_tokens": spec.get("max_tokens", 2000),
    }
    return messages, params