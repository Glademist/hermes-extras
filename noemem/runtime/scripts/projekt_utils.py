"""Shared project naming + catalog for dreamer / projektovy_updater."""
from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_RUNTIME_DIR = _SCRIPTS_DIR.parent
sys.path.insert(0, str(_RUNTIME_DIR))

from noemem_paths import CATALOG_FILE, PROJEKTY_WIKI_DIR  # noqa: E402

PROJEKTY_DIR = PROJEKTY_WIKI_DIR


def canonicalize_projekt(projekt: str) -> str:
    """Sjednotí PROJEKT na kanonický stem (bez denní přípony -YYYYMMDD)."""
    projekt = (projekt or "").strip()
    if not projekt or projekt.upper().startswith("NOV"):
        return projekt
    while re.search(r"-\d{8}$", projekt):
        projekt = re.sub(r"-\d{8}$", "", projekt)
    return projekt


def load_catalog_slugs() -> list[str]:
    """Kanonické projekty z project_catalog.md (authoritative source).

    B: katalog je jediný zdroj projektu — ne ~/Projekty/*.md. Slug v katalogu
    znamená, že ho model smí použít jako PROJEKT (i malé projekty, hry, one-offs).
    """
    return sorted(load_project_catalog().keys())


def load_prompt_projects() -> list[str]:
    """Legacy kanonické projekty — *.md v ~/Projekty bez -YYYYMMDD v názvu.

    Pouze jako fallback merge pro zpětnou kompatibilitu; primární zdroj je
    project_catalog.md (viz load_catalog_slugs).
    """
    if not PROJEKTY_DIR.exists():
        return []
    stems = sorted(f.stem for f in PROJEKTY_DIR.glob("*.md") if not f.name.startswith("_"))
    return sorted(s for s in stems if not re.search(r"-\d{8}", s))


def load_project_catalog(known_slugs: list[str] | None = None) -> dict[str, str]:
    """Načte slug → 1–2 věty z project_catalog.md. Jen záznamy pro known_slugs pokud zadané."""
    if not CATALOG_FILE.exists():
        return {}
    text = CATALOG_FILE.read_text(encoding="utf-8", errors="ignore")
    catalog: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("```"):
            continue
        m = re.match(r"^([a-z0-9][a-z0-9-]*)\s*:\s*(.+)$", line, re.I)
        if not m:
            continue
        slug = canonicalize_projekt(m.group(1).lower())
        if not slug or slug.startswith("nov"):
            continue
        catalog[slug] = m.group(2).strip()
    if known_slugs:
        want = {canonicalize_projekt(s).lower() for s in known_slugs}
        return {k: v for k, v in catalog.items() if k.lower() in want}
    return catalog


def format_catalog_for_prompt(catalog: dict[str, str]) -> str:
    if not catalog:
        return ""
    lines = ["Katalog projektů (slug → definice):"]
    for slug in sorted(catalog):
        lines.append(f"- {slug}: {catalog[slug]}")
    return "\n".join(lines)


# --- deterministický projektový validátor -------------------------------------
# Proč: model opisoval příklad z promptu ("NOVÝ: nazev") nebo si vymýšlel projekt
# pro jednorázovku; fallback byl vždy hermes-noemem, což míchalo model/inference
# práci do paměťového systému. Tenhle krok běží PO LLM a rozhoduje deterministicky.

_PLACEHOLDER_RE = re.compile(
    r"^nov[ýy]?\s*:?\s*(n[aá]z?e?v|name|slug|projekt|xxx|todo)?\s*[.!]?$", re.I)
_NEW_PROJECT_RE = re.compile(r"^nov[ýy]?\s*:\s*([a-z0-9][a-z0-9-]{2,40})$", re.I)


def _stem(word: str) -> str:
    """Diacritic-free 5-char stem — Czech inflects, so 'kalorie' must match 'kalorii'."""
    word = unicodedata.normalize("NFKD", word.lower())
    word = "".join(c for c in word if not unicodedata.combining(c))
    return word[:5]


def catalog_keywords() -> dict[str, str]:
    """stem -> slug, parsed from `Klíčová slova: a, b` inside catalog definitions."""
    kw2slug: dict[str, str] = {}
    for slug, desc in load_project_catalog().items():
        m = re.search(r"(?:klíčová slova|keywords)\s*:\s*(.+)$", desc, re.I)
        if not m:
            continue
        for kw in re.split(r"[,\s]+", m.group(1).strip()):
            kw = kw.strip()
            if len(kw) > 3:
                kw2slug.setdefault(_stem(kw), slug)
    return kw2slug


def resolve_project(raw: str, session_text: str = "", catalog: dict[str, str] | None = None) -> tuple[str, str]:
    """LLM project value -> (canonical project, reason) — deterministic, never invents a project.

    reasons: ok | catalog | new-proposal | placeholder-mapped | keyword | unclassified
    """
    catalog = catalog if catalog is not None else load_project_catalog()
    value = (raw or "").strip().strip('"').strip("'")
    value = re.sub(r"\s+", " ", value)

    if not catalog:
        # No catalog (missing/unreadable): we cannot resolve anything — keep the model's
        # value instead of turning every session into "unclassified" in silence.
        print("  ⚠️  project guard: catalog empty/unreadable — keeping the model's value")
        return value or "unclassified", "catalog-missing"

    if value.lower() in catalog:
        return value.lower(), "catalog"
    # placeholder first: "NOVÝ: nazev" would otherwise pass as a plausible proposal slug
    if value and not _PLACEHOLDER_RE.match(value) and _NEW_PROJECT_RE.match(value):
        return value, "new-proposal"          # keep the proposal for review, never auto-approved

    # empty, off-catalog, or a copied example -> decide from the session text
    reason = "placeholder-mapped" if (not value or _PLACEHOLDER_RE.match(value)) else "keyword"
    words = {_stem(w) for w in re.findall(r"[^\W_]{4,}", (session_text or "").lower(), re.UNICODE)}
    hits: dict[str, int] = {}
    for kw_stem, slug in catalog_keywords().items():
        if kw_stem in words:
            hits[slug] = hits.get(slug, 0) + 1
    if hits:
        best = max(hits, key=lambda s: (hits[s], len(s)))
        return best, reason
    return "unclassified", "unclassified"