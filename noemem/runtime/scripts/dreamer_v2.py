#!/usr/bin/env python3
"""Dreamer v2 — tier-aware historický analytik.

Krátké session: tiered_anchor_fidelity (1× LLM). XL session (≥10k znaků):
multipass_anchor_fidelity (extract → write → verify retry).
Ukládá do ~/hermes-noemem/archive/. Mapuje na projekty z ~/Projekty/.

Použití:
  python3 dreamer_v2.py                     # všechny nové session
  python3 dreamer_v2.py --new-only           # jen nezpracované
  python3 dreamer_v2.py --session-id <key>   # jedna konkrétní
"""
import json, argparse, re, sys, time, unicodedata, os
from datetime import datetime
from pathlib import Path

from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
_RUNTIME_DIR = _SCRIPTS_DIR.parent
sys.path.insert(0, str(_RUNTIME_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR))
from noemem_paths import (  # noqa: E402
    ARCHIVE_DIR,
    HERMES_SESSIONS_DIR,
    LEGACY_DREAMER_STATE,
    LEGACY_SESSION_INDEX,
    NOEMEM_ROOT,
    PROJEKTY_CURRENT_DIR,
    PROJEKTY_WIKI_DIR,
    DREAMER_STATE_FILE,
    SESSION_INDEX_FILE,
    migrate_state_file,
    role_config,
)
from dreamer_tiers import is_garbage_anchor, session_tier, tier_size_preamble  # noqa: E402
from multipass_dreamer import run_multipass_dreamer  # noqa: E402
from prompt_variants import dreamer_prompt_variant  # noqa: E402
from api_lock import with_api_lock
from mtplx_client import (
    active_model_id,
    parse_assistant_content,
    post_chat,
    prepare_chat_body,
)
from session_clean import clean_session
from session_source import content_timestamp, load_session_content, _message_digest as message_digest
from projekt_utils import (
    canonicalize_projekt,
    format_catalog_for_prompt,
    load_catalog_slugs,
    load_project_catalog,
    load_prompt_projects,
    resolve_project,
)

# === hermes-noemem layer constants (noemem_paths) ===
PROJEKTY_DIR = PROJEKTY_WIKI_DIR
SESSIONS_DIR = HERMES_SESSIONS_DIR
STATE_FILE = migrate_state_file(LEGACY_DREAMER_STATE, DREAMER_STATE_FILE)
migrate_state_file(LEGACY_SESSION_INDEX, SESSION_INDEX_FILE)

# Teacher disk ground truth (the real source for counts, per assemblies/ + topics/*.json registries)
TEACHER_ROOT = Path("~/AI/ExampleProject")
TEACHER_TOPICS = TEACHER_ROOT / "topics"
TEACHER_ASSEMBLIES = TEACHER_ROOT / "assemblies"

DREAMER_SYSTEM = (
    "You are the EDITOR of a long-term memory archive. Your only test for "
    "every sentence: will it still be true and useful next month? End states, "
    "decisions, durable facts. Never the journey."
)

# Model/token config from runtime/model_config.json (role "dreamer"). Falls back
# to these defaults if the file is missing or the role is absent.
DREAMER_MAX_TOKENS = 8192
# Floor on the COMPLETION budget for every dreamer call (name was DREAMER_THINKING_MIN_TOKENS
# when it only guarded the thinking case; it is really "a pass must have room to write").
DREAMER_MIN_TOKENS = 8192
DREAMER_TEMPERATURE = 0.2
# Reasoning OFF for archive analysis. model_config.json role "dreamer" says thinking:false
# to stop CoT leaking into the durable text — but the code never read that key, and the
# two analysis call sites defaulted enable_thinking=True, so the dreamer ran WITH thinking
# (which was silently on anyway, since Nous ignores enable_thinking). Read it and pass it.
DREAMER_THINKING = False
MAX_SESSION_CHARS = 14000
# Extra attempts after an unusable reply (empty body or a reply cut mid-file). One was
# not enough: a transient finish_reason="error" failed the single retry too and the file
# was left unchanged (hermes-system.md, 2026-10-08 14:01).
MAX_UNUSABLE_RETRIES = 2

# Set by call_llm_messages on every call: the finish_reason and completion_tokens
# of the LAST response. A file-rewriting caller must check _LAST_FINISH_REASON ==
# "length" — such a response is non-empty and looks plausible but is cut
# mid-sentence, so writing it silently destroys the tail of the file.
_LAST_FINISH_REASON = None      # type: Optional[str]
_LAST_COMPLETION_TOKENS = None  # type: Optional[int]

_DREAMER_CFG = role_config("dreamer") or {}
if _DREAMER_CFG:
    DREAMER_MAX_TOKENS = int(_DREAMER_CFG.get("max_tokens", DREAMER_MAX_TOKENS))
    DREAMER_MIN_TOKENS = int(_DREAMER_CFG.get("max_tokens", DREAMER_MIN_TOKENS))
    DREAMER_TEMPERATURE = float(_DREAMER_CFG.get("temperature", DREAMER_TEMPERATURE))
    DREAMER_THINKING = bool(_DREAMER_CFG.get("thinking", DREAMER_THINKING))


def load_sessions_index() -> dict:
    with open(SESSIONS_DIR / "sessions.json", "r", encoding="utf-8") as f:
        return json.load(f)


def iter_session_meta(index: dict):
    """Yield (session_key, meta) from sessions.json; skip _README and other non-dict entries."""
    for session_key, meta in index.items():
        if session_key.startswith("_"):
            continue
        if not isinstance(meta, dict):
            print(
                f"⚠️  sessions.json: přeskočen neplatný záznam {session_key!r} "
                f"(typ {type(meta).__name__}, očekáván dict)"
            )
            continue
        yield session_key, meta


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_run_at": None, "processed": {}}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def load_session_index() -> dict:
    if SESSION_INDEX_FILE.exists():
        with open(SESSION_INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_session_index(index: dict):
    SESSION_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSION_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)


_DATED_PROJECT_SUFFIX = re.compile(r"-\d{8}(-\d{8})?$")


def session_anchor_tokens(session_text: str) -> set[str]:
    """Cheap anchors from session for prompt injection.

    Case is PRESERVED — these anchors go into the prompt as mandatory
    backtick-quoted details that must be copied verbatim into VÝSLEDEK.
    Lowercasing here ships lowercase paths into the archive.
    """
    anchors: set[str] = set()
    for m in re.finditer(r"`([^`]{3,80})`", session_text):
        anchors.add(m.group(1))
    for m in re.finditer(r"/(?:Users|\.hermes|Projekty)[^\s`\"']{3,60}", session_text):
        anchors.add(m.group(0))
    for m in re.finditer(
        r"\b(?:mtplx|dreamer|qmd|hermes-noemem|launchctl|state\.db)\b",
        session_text,
        re.I,
    ):
        anchors.add(m.group(0))
    return anchors


def prompt_anchors(session_text: str, n: int = 8) -> list[str]:
    """Pick high-signal session anchors for anti_verbosity_anchors prompt."""
    scored: list[tuple[float, str]] = []
    for anchor in session_anchor_tokens(session_text):
        a = anchor.strip()
        if len(a) < 4 or is_garbage_anchor(a):
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


def build_dreamer_prompt(
    clean_text: str,
    session_start: str,
    known_projects: list[str],
    candidates_text: str = "",
) -> str:
    """Tier-aware anchor fidelity prompt (single-pass for short/medium/long)."""
    projekty_str = ", ".join(known_projects) if known_projects else "(žádné)"
    ts = session_start[:19]
    char_count = len(clean_text)
    tier = session_tier(char_count)
    anchor_block = ""
    anchors = prompt_anchors(clean_text, n=tier.inject_anchor_count)
    if anchors:
        lines = "\n".join(f"- `{a}`" for a in anchors)
        anchor_block = f"""
Mandatory details from the session (each must appear in vysledek, in `backticks`):
{lines}"""
    rules = f"""{tier_size_preamble(tier, char_count)}
Strict limits:
- vysledek: {tier.vysledek_vety_prompt}. Every sentence contains at least one mandatory detail below (in `backticks`).
- ponauceni: {tier.ponauceni_vety_prompt}.
- projekt: the PROJEKT line (canonical stem without -YYYYMMDD).
- NO internal tool IDs (e.g. TOOL[call_...], call_<hash>, tool_call_id) — never copy them into any field; replace them with a description of what the tool did (e.g. "the file-reading tool").
- PATH RULE (hard): copy every file path VERBATIM from the session — exact casing as on disk. A path that does not exist on disk fails archive lint and quarantines the record. No `...`/ellipsis truncation (full path or nothing); if you are not certain of the exact path, describe the file instead of guessing.{anchor_block}"""
    return f"""You are an EDITOR of a long-term memory archive. Each session is a record of interaction between the user (the user) and his AI agent.
Your archive will be read NEXT MONTH, when context is gone. Someone will ask: "what is the state of this project, what was decided, what must I not redo?"

The only question that matters for every sentence you write:
  WILL THIS STILL BE TRUE AND USEFUL NEXT MONTH?

Write in English regardless of the language the session was conducted in. Keep proper nouns, file paths, commands and verbatim Czech quotes as-is.

Session started: {ts}

For each session determine:

- timestamp: use session_start ({ts})
- tema: a short title (3–8 words) of what was discussed (used for the archive filename)
- proc: what the request or problem was, what led to it
- vysledek: END STATE only — decisions made, project state changes, durable facts (paths of lasting artifacts, services, configs, crons that persist, measured capabilities, deadlines). Test each sentence: still true and useful next month? If not, delete it. Do NOT narrate the journey: no attempts, retries, failures of tooling, install steps, command outputs, timings of test runs, or things tried and superseded within the session. Exception: a failure IS memory only if the failed approach itself is a durable lesson (e.g. "tool X cannot do Y — use Z instead").
- ponauceni: the transferable lesson, if any. If the session produced no transferable lesson, write "none". Not "documentation was updated".
- kategorie: personal | projects | maintenance | knowledge
- projekt: canonical name of an existing project (without the -YYYYMMDD suffix) from: {projekty_str}. Ignore daily snapshots in the list — the date belongs only in timestamp, the archive filename is handled by the script. Example: hermes-noemem, not hermes-noemem-20260528. If nothing fits, write `NOVÝ:` followed by a short lowercase slug you propose (letters, digits, hyphens only) — never copy any example from this prompt, and never propose a project for a one-off task.
- PROJECT GUARD (important): do not pick a project just because its slug appears in the session text. Assign by the MEANING of the work (model/inference work → local-api; memory system → hermes-noemem; teaching → teacher). One-off / small work is NOT a new project — assign to the closest existing project, even if imperfect. A proposal is only for a NEW RECURRING work area with no existing home.
- supersedes: only files of the SAME project as projekt above, or a previous archive of this same session (candidates below). Never a different project. Otherwise empty.
{rules}
NOISE FILTER (editorial inclusion test, replaces the old list): apply "will this still be true and useful next month?" to every candidate fact. Approval-gate failures, retries, timeouts, pip/venv trivia, cron test-run timings, clone sizes, "a tool was called", path-casing differences — all fail the test and are excluded even though factually correct.

=== EXAMPLES ===

Example 1 — GOOD record (the standard to hit):
Session gist: the user asked whether laya-mlx could help hermes-noemem; it turned into integrating laya into prompt-guard.
Good output:
- vysledek: "Laya-MLX (tiny ModernBERT typed classifier, ~13 ms, no text generation) is not useful for noemem archival but was wired into prompt-guard as a watch-only third stage: laya server (`~/AI/example-spike/laya_server.py`, port 8097, launchd `ai.laya.guard-server.plist`), fail-open client `laya_backend.py` (disabled via `PROMPT_GUARD_LAYA=off`), every scan verdict logged to `~/hermes-noemem/logs/laya-guard.jsonl`. Measured: binary benign/attack 14/16 with 0 false positives — usable as cheap always-on first stage; confidence NOT usable as abstain signal. Daily digest cron `laya-guard-watch-digest` (20:30, no_agent) reports server health + verdict mix. Arbiter (same models, better serving) cloned to `~/AI/example-classifier`, deliberately NOT wired — decision gated on ~1 week of digest data, review from 2026-09-24. Keep → upgrade to arbiter; kill → remove cron+plist+guard stage."
- ponauceni: "laya-mlx is not on the PyPI CDN — install the wheel directly; needs Python >= 3.11. Watch-only staging before enforcement is the pattern for any new classifier in the guard."
Why good: every sentence is a durable end-state fact with a concrete handle (path, port, cron, number, deadline). Nothing about pip failures, venv versions, test-run durations, or which tool was called.

Example 2 — BAD record (do not do this):
- vysledek: "The initial installation at `~/AI/example-spike` failed because its venv used Python 3.9.6 and pip 21.2.4 ... Its manual cron execution completed successfully in 0.68 seconds with zero API calls ... `~/AI/example-classifier` was cloned at commit `5a178dd` (v0.2.0), about 22 MB ... `laya server: up` showed that the server was running."
Why bad: pip/venv trivia, a 0.68 s manual test run, clone size, and "server is up" are all stale within a week — none pass the next-month test. All of it is excluded even though it is factually correct.
=== END EXAMPLES ===

Format (respond with ONLY one valid JSON object, no other text before or after — keys exactly as follows, values in English):
{{
  "timestamp": "{ts}",
  "tema": "short title (3-8 words)",
  "proc": "what the request or problem was",
  "vysledek": "what actually happened ({tier.vysledek_vety_prompt}). Every sentence at least one mandatory detail (in backticks).",
  "ponauceni": "what to take from it ({tier.ponauceni_vety_prompt}).",
  "kategorie": "personal|projects|maintenance|knowledge",
  "projekt": "canonical name of an existing project (without -YYYYMMDD)",
  "supersedes": "list of .md files (empty [] if none)"
}}
NO text outside the JSON. Do not copy internal tool IDs (TOOL[call_...], call_<hash>) into any value — replace them with a description of what the tool did.

{candidates_text}
Session:
{clean_text}"""


def patch_frontmatter(text: str, updates: dict) -> str:
    """Merge keys into YAML frontmatter (simple line-based, no PyYAML)."""
    m = re.match(r"^(---\s*\n)(.*?)(\n---)", text, re.DOTALL)
    if not m:
        return text
    keys = {k.lower() for k in updates}
    lines = []
    for line in m.group(2).split("\n"):
        if ":" in line and line.split(":", 1)[0].strip().lower() in keys:
            continue
        lines.append(line)
    for k, v in updates.items():
        lines.append(f"{k}: {v}")
    return f"{m.group(1)}{chr(10).join(lines)}{m.group(3)}" + text[m.end() :]


def slugify(text: str) -> str:
    """Vytvoří filename-safe slug z tématu.

    Používá NFKD normalizaci (stdlib unicodedata) pro správné odstranění diakritiky:
    "přírodověda" → "prirodoveda" (ne "prodovda"), "odblokování" → "odblokovani" atd.
    Omezuje na ~6 slov, aby byl název souboru čitelný a užitečný pro agenta
    (plný popis zůstává uvnitř souboru v TÉMA: řádku + frontmatteru).
    """
    if not text:
        return "session"
    # Správná diakritika → ASCII (p, ř, í, ě, š, č, ř, ž, ý, á, í, é, ó, ú, ů, ď, ť, ň ...)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text.strip())
    # Max ~6 slov (lepší než tvrdé znaky — zachová celá slova)
    words = [w for w in text.split("-") if w][:6]
    text = "-".join(words)
    return text or "session"


@with_api_lock
def call_llm_messages(
    messages: list[dict],
    *,
    max_tokens: int = DREAMER_MAX_TOKENS,
    temperature: float = DREAMER_TEMPERATURE,
    enable_thinking: bool = True,
) -> Optional[str]:
    """LLM call s hotovými messages (tiered_anchor_fidelity / multipass)."""
    # Floor the completion budget UNCONDITIONALLY. It used to apply only when thinking was
    # on, which quietly propped up the multipass passes: they request small per-pass budgets
    # (extract 2200, write 3000) that are far below what a full 14k-char session needs. With
    # thinking on, the floor hid that; turning thinking off exposed it and one run produced
    # 12 truncations where the previous day had NONE (2026-10-09 03:00 vs 10-08). A pass needs
    # room to WRITE its output whether or not it reasons, and a flaky backend can still emit
    # reasoning despite reasoning_effort="none". The floor only costs money if it is used.
    token_budget = max(max_tokens, DREAMER_MIN_TOKENS)
    body = prepare_chat_body(
        {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": token_budget,
            "enable_thinking": enable_thinking,
        }
    )
    try:
        resp = post_chat(body, timeout=900)
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        global _LAST_FINISH_REASON, _LAST_COMPLETION_TOKENS
        _LAST_FINISH_REASON = choice.get("finish_reason")
        _LAST_COMPLETION_TOKENS = (data.get("usage") or {}).get("completion_tokens")
        payload = choice["message"]
        text = parse_assistant_content(payload)
        # finish_reason "length" means the response was cut mid-sentence even though
        # it is non-empty and looks plausible. Callers that rewrite a file MUST know:
        # a silently truncated rewrite loses the tail of the file.
        if _LAST_FINISH_REASON == "length":
            print(f"  ⚠️  Odpověď OŘÍZNUTA (finish_reason=length, "
                  f"max_tokens={token_budget}, completion={_LAST_COMPLETION_TOKENS})")
        # Retry on an UNUSABLE reply, up to MAX_UNUSABLE_RETRIES extra attempts with a
        # short backoff. Two shapes:
        #   * empty  — reasoning tokens ate max_tokens (HTTP 200, content="") OR a
        #              transient backend error (finish_reason="error").
        #   * length — the reply was cut mid-file; non-empty and plausible, so it
        #              passes every length-based gate but loses the file's tail.
        # Escalate the ceiling each attempt. A transient error needs the retry more than
        # the headroom, and one retry was not enough (hermes-system.md, 2026-10-08 14:01:
        # both attempts came back empty and the file was left unchanged).
        # CRITICAL: refresh the flags from each attempt's OWN response. They describe the
        # text we return, never a discarded earlier attempt. Leaving them stale made
        # memory_consolidate reject a GOOD escalated retry as "truncated" and drop the
        # file's whole night of facts (projects.md, 2026-10-07 13:49 and 10-08 03:11).
        backoff = 3.0
        for _ in range(MAX_UNUSABLE_RETRIES):
            if text and _LAST_FINISH_REASON != "length":
                break
            esc = max(token_budget * 2, token_budget + 2000)
            reasoning = ((data.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")
            print(f"  ⚠️  Prázdná/říznutá odpověď (max_tokens={token_budget}, "
                  f"finish_reason={_LAST_FINISH_REASON}"
                  f"{f', reasoning_tokens={reasoning}' if reasoning is not None else ''})"
                  f" — zkouším znovu za {backoff:.0f}s s max_tokens={esc}...")
            time.sleep(backoff)
            backoff *= 2
            token_budget = esc
            body["max_tokens"] = esc
            resp = post_chat(body, timeout=900)
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            _LAST_FINISH_REASON = choice.get("finish_reason")
            _LAST_COMPLETION_TOKENS = (data.get("usage") or {}).get("completion_tokens")
            text = parse_assistant_content(choice["message"])
        return text if text else None
    except Exception as e:
        print(f"  ❌ LLM error: {e}")
        return None


def call_llm(
    prompt: str,
    *,
    system: str = DREAMER_SYSTEM,
    max_tokens: int = DREAMER_MAX_TOKENS,
    temperature: float = DREAMER_TEMPERATURE,
    enable_thinking: bool = True,
) -> Optional[str]:
    """Jeden LLM call na MTPLX (legacy single-string prompt)."""
    return call_llm_messages(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
        enable_thinking=enable_thinking,
    )


def _mp_chat_adapter(_model, messages, params, **kwargs):
    del _model, kwargs
    return {
        "content": call_llm_messages(
            messages,
            max_tokens=int(params.get("max_tokens", DREAMER_MAX_TOKENS)),
            temperature=float(params.get("temperature", DREAMER_TEMPERATURE)),
            enable_thinking=DREAMER_THINKING,
        )
        or "",
    }


def _inject_candidates(messages: list[dict], candidates_text: str) -> list[dict]:
    if not candidates_text.strip():
        return messages
    out = list(messages)
    user = out[1]["content"]
    if "\nSession:\n" in user:
        user = user.replace("\nSession:\n", f"\n{candidates_text.strip()}\n\nSession:\n", 1)
    else:
        user = user + "\n" + candidates_text.strip()
    out[1] = {**out[1], "content": user}
    return out


def run_dreamer_for_session(
    clean_text: str,
    session_start: str,
    known_projects: list[str],
    candidates_text: str = "",
) -> tuple[Optional[str], dict]:
    """tiered_anchor_fidelity (1-pass) nebo multipass_anchor_fidelity (xl)."""
    char_count = len(clean_text)
    tier = session_tier(char_count)
    meta: dict = {"tier": tier.label, "char_count": char_count, "multipass": tier.multipass}

    if tier.multipass:
        fixture = {
            "clean_text": clean_text,
            "session_start": session_start,
            "char_count": char_count,
            "extra_preamble": candidates_text.strip(),
        }
        mp = run_multipass_dreamer(
            fixture,
            _mp_chat_adapter,
            "",
            known_projects=known_projects,
        )
        meta.update(
            {
                "verify_retry": mp.get("verify_retry"),
                "missing_anchors_after": mp.get("missing_anchors_after"),
                "pass1_facts": len(mp.get("pass1_facts") or []),
            }
        )
        return mp.get("content"), meta

    fixture = {
        "clean_text": clean_text,
        "session_start": session_start,
        "char_count": char_count,
    }
    messages, params = dreamer_prompt_variant(fixture, "tiered_anchor_fidelity", known_projects)
    messages = _inject_candidates(messages, candidates_text)
    clean_params = {k: v for k, v in params.items() if not str(k).startswith("_")}
    output = call_llm_messages(
        messages,
        max_tokens=int(clean_params.get("max_tokens", DREAMER_MAX_TOKENS)),
        temperature=float(clean_params.get("temperature", DREAMER_TEMPERATURE)),
        enable_thinking=DREAMER_THINKING,
    )
    return output, meta


def find_candidate_memories_for_project(projekt: str, max_count: int = 3) -> list[str]:
    """Najde nedávné záznamy ze stejného projektu, které by tato session mohla nahrazovat (supersedes).

    Jednoduché a levné (project-first díky konzistentnímu tagování po vylepšení názvů).
    Vrací krátký seznam "filename — preview" pro prompt.
    Používá NOEMEM_ROOT / ARCHIVE_DIR (připraveno pro budoucí extrakci mimo ~/.hermes).
    """
    if not projekt or projekt.upper().startswith("NOV"):
        return []

    candidates = []
    try:
        for path in sorted(ARCHIVE_DIR.glob("*.md"), reverse=True)[:80]:
            if path.name.startswith("_"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")[:800]
                if f"project: {projekt}" in text or projekt.lower() in path.name.lower():
                    rel = path.name
                    m = re.search(r'TÉMA:\s*(.+)', text)
                    preview = (m.group(1).strip()[:60] if m else "")
                    candidates.append(f"{rel} — {preview}")
                    if len(candidates) >= max_count:
                        break
            except Exception:
                pass
    except Exception:
        pass
    return candidates


def read_archive_meta(filename: str) -> dict:
    """Frontmatter fields from an archive file (best-effort)."""
    p = ARCHIVE_DIR / filename
    if not p.exists():
        return {}
    head = p.read_text(encoding="utf-8", errors="ignore")[:4000]
    meta = {}
    for key in ("project", "source_session_id", "retrieval_tier"):
        m = re.search(rf"^{key}:\s*(.+)$", head, re.M)
        if m:
            meta[key] = m.group(1).strip()
    return meta


def resolve_valid_supersedes(
    projekt: str,
    session_id: str,
    new_filename: str,
    llm_supersedes: list,
    index_prev: Optional[str],
) -> list[str]:
    """Povolené supersedes: session_index prev, stejná session_id, nebo stejný project."""
    canon = canonicalize_projekt(projekt).lower()
    out: list[str] = []
    seen: set[str] = set()

    def allow(name: str) -> bool:
        if not name or name == new_filename or name in seen:
            return False
        if not (ARCHIVE_DIR / name).exists():
            return False
        old = read_archive_meta(name)
        old_proj = canonicalize_projekt(old.get("project", "")).lower()
        old_sid = old.get("source_session_id", "")
        if index_prev and name == index_prev:
            return True
        if old_sid and old_sid == session_id:
            return True
        if canon and old_proj and old_proj == canon:
            return True
        return False

    if index_prev and allow(index_prev):
        out.append(index_prev)
        seen.add(index_prev)

    raw = llm_supersedes or []
    if isinstance(raw, str):
        raw = [raw] if raw.strip() else []
    for name in raw:
        name = (name or "").strip()
        if not name.endswith(".md"):
            continue
        if allow(name):
            out.append(name)
            seen.add(name)
        if len(out) >= 3:
            break

    dropped = [n for n in raw if isinstance(n, str) and n.strip() and n.strip() not in out]
    if dropped:
        print(
            "   ⛔ supersedes odmítnuto (jiný projekt/session): "
            + ", ".join((n.strip()[:60] for n in dropped[:5]))
        )
    return out[:3]


def parse_output(text: str) -> Optional[dict]:
    """Parses LLM output into a dict.

    Primary: treat as JSON (the dreamer prompt now asks for a JSON object). Extract the
    first balanced {...} block and json.loads it — tolerant to surrounding markdown fences
    or stray text. Fallback: the legacy colon-line parser (TÉMA:/PROČ:/...) so cloud
    backends / older models that ignore the JSON instruction still work.
    """
    result = None
    if text and text.strip():
        result = _parse_output_json(text)
    if result is None:
        result = _parse_output_colon(text)
    return result


def _parse_output_json(text: str) -> Optional[dict]:
    """Try to parse output as JSON. Returns dict or None."""
    import json as _json

    def _try(s: str):
        if not s.strip():
            return None
        try:
            return _json.loads(s)
        except Exception:
            return None

    # 1) exact
    r = _try(text)
    if r is not None:
        return _normalize_json_result(r)
    # 2) strip markdown code fences
    import re as _re
    m = _re.search(r"```(?:json)?\s*(.*?)\s*```", text, _re.DOTALL)
    if m:
        r = _try(m.group(1))
        if r is not None:
            return _normalize_json_result(r)
    # 3) first balanced {...} block
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    r = _try(text[start:i + 1])
                    if r is not None:
                        return _normalize_json_result(r)
                    break
    return None


def _normalize_json_result(raw: dict) -> Optional[dict]:
    """Normalize a JSON-parsed dict to the dreamer result schema; None if empty."""
    if not isinstance(raw, dict):
        return None
    get = lambda *ks: next((v for k in ks if isinstance(v := raw.get(k), str) and v.strip()), "")

    result = {
        "timestamp": get("timestamp", "TIMESTAMP", "Timestamp"),
        "tema": get("tema", "TÉMA", "TEMA", "title"),
        "proc": get("proc", "PROČ", "PROC", "reason"),
        "vysledek": get("vysledek", "VÝSLEDEK", "VYSLEDEK", "result"),
        "ponauceni": get("ponauceni", "PONAUČENÍ", "PONAUCENI", "lesson"),
        "kategorie": get("kategorie", "KATEGORIE", "category"),
        "projekt": get("projekt", "PROJEKT", "project"),
        "supersedes": raw.get("supersedes") or raw.get("SUPERSEDES") or [],
    }
    if not result["tema"] and not result["proc"] and not result["vysledek"]:
        return None

    if result["kategorie"] not in ("personal", "projects", "maintenance", "knowledge"):
        result["kategorie"] = "personal"

    # Normalize supersedes to clean list of .md filenames
    sup = result["supersedes"]
    if isinstance(sup, str):
        sup = [sup]
    parts = [p.strip() for p in sup if isinstance(p, str) and p.strip() and p.strip().endswith(".md")]
    result["supersedes"] = parts[:3]
    return result


def _parse_output_colon(text: str) -> Optional[dict]:
    """Legacy colon-line parser (TÉMA:/PROČ:/...). Used as fallback."""
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
        upper = stripped.upper()
        if stripped.startswith("TIMESTAMP:"):
            result["timestamp"] = stripped[10:].strip()
            current_field = "timestamp"
        elif stripped.startswith("TÉMA:"):
            result["tema"] = stripped[5:].strip()
            current_field = "tema"
        elif stripped.startswith("TEMA:"):  # bez diakritiky fallback
            result["tema"] = stripped[5:].strip()
            current_field = "tema"
        elif stripped.startswith("PROČ:"):
            result["proc"] = stripped[5:].strip()
            current_field = "proc"
        elif stripped.startswith("PROC:"):  # bez diakritiky
            result["proc"] = stripped[5:].strip()
            current_field = "proc"
        elif stripped.startswith("VÝSLEDEK:"):
            current_field = "vysledek"
            rest = stripped[10:].strip()
            if rest:
                field_buffers["vysledek"].append(rest)
        elif stripped.startswith("VYSLEDEK:"):
            current_field = "vysledek"
            rest = stripped[9:].strip()
            if rest:
                field_buffers["vysledek"].append(rest)
        elif stripped.startswith("PONAUČENÍ:"):
            current_field = "ponauceni"
            rest = stripped[10:].strip()
            if rest:
                field_buffers["ponauceni"].append(rest)
        elif stripped.startswith("PONAUCENI:"):
            current_field = "ponauceni"
            rest = stripped[10:].strip()
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
        elif current_field in ("vysledek", "ponauceni") and not stripped and field_buffers[current_field]:
            # Prázdný řádek = konec odstavce u polí, kde čekáme odstavec
            pass

    # Složit buffery
    for key in ("vysledek", "ponauceni"):
        if field_buffers[key]:
            result[key] = "\n".join(field_buffers[key])

    # Validace
    if not result["tema"] and not result["proc"] and not result["vysledek"]:
        return None

    # Kategorie fallback
    if result["kategorie"] not in ("personal", "projects", "maintenance", "knowledge"):
        result["kategorie"] = "personal"

    # Normalize supersedes to clean list of .md filenames (support comma/space separated)
    sup = result.get("supersedes", "") or ""
    if isinstance(sup, str) and sup:
        parts = [p.strip() for p in re.split(r'[,;\s]+', sup) if p.strip() and p.strip().endswith(".md")]
        result["supersedes"] = parts[:3]
    else:
        result["supersedes"] = []

    return result


def save_archive(result: dict, session_id: str, updated_at: str):
    """Uloží výsledek do ~/hermes-noemem/archive/."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    ts = result["timestamp"][:10] if result["timestamp"] else datetime.now().strftime("%Y-%m-%d")
    # Prefix souboru = den zpracování (updated_at session), aby nightly našel čerstvé záznamy.
    if updated_at and len(updated_at) >= 10:
        ts = updated_at[:10]
    projekt = (result.get("projekt") or "").strip()
    tema = result.get("tema", "") or ""

    # Lepší naming convention: project jako hlavní identifikátor (jako v Projekty/),
    # + krátký rozlišovač z tématu. Agent pak v ls / QMD výsledcích hned vidí "teacher-..." + téma.
    pslug = slugify(projekt) if projekt and not projekt.upper().startswith("NOV") else ""
    tslug = slugify(tema)

    # Odstraň trailing -YYYYMMDD z projektu (datum už máme v prefixu souboru)
    pslug = re.sub(r"-\d{8}$", "", pslug)

    if pslug:
        if tslug and tslug != pslug:
            slug = f"{pslug}-{tslug}"
        else:
            slug = pslug
    else:
        slug = tslug or "session"

    # Bezpečné omezení (velmi dlouhé názvy nejsou potřeba — obsah uvnitř je plný)
    if len(slug) > 85:
        slug = slug[:85].rsplit("-", 1)[0]

    filename = f"{ts}_{slug}.md"
    filepath = ARCHIVE_DIR / filename

    index_prev = None
    try:
        idx = load_session_index()
        index_prev = (idx.get(session_id) or {}).get("last_archive_docid")
    except Exception:
        pass

    raw_sup = result.get("supersedes", []) or []
    if isinstance(raw_sup, str):
        raw_sup = [raw_sup] if raw_sup.strip() else []
    supersedes = resolve_valid_supersedes(
        projekt, session_id, filename, raw_sup, index_prev
    )

    # Vypsat do konzole
    print(f"\n📄 {filename}")
    print(f"   TÉMA: {result['tema'][:80]}")
    print(f"   PROJEKT: {result['projekt']}")
    if supersedes:
        print(f"   SUPERSEDES: {', '.join(supersedes)}")

    # Sestavit soubor (přidat supersedes do frontmatteru, pokud nějaké)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("---\n")
        f.write(f"created: {result['timestamp'] or datetime.now().isoformat()}\n")
        f.write(f"source_session_id: {session_id}\n")
        f.write(f"source_updated_at: {updated_at}\n")
        f.write(f"category: {result['kategorie']}\n")
        f.write(f"project: {result['projekt']}\n")
        f.write(f"importance: 7\n")
        f.write("retrieval_tier: live\n")
        if supersedes:
            f.write(f"supersedes: {', '.join(supersedes)}\n")
        f.write("---\n\n")
        f.write(f"TIMESTAMP: {result['timestamp']}\n")
        f.write(f"TÉMA: {result['tema']}\n")
        f.write(f"PROČ: {result['proc']}\n")
        f.write(f"VÝSLEDEK: {result['vysledek']}\n" if result['vysledek'] else "VÝSLEDEK:\n")
        f.write(f"PONAUČENÍ: {result['ponauceni']}\n" if result['ponauceni'] else "PONAUČENÍ:\n")
        f.write(f"KATEGORIE: {result['kategorie']}\n")
        f.write(f"PROJEKT: {result['projekt']}\n")
        if supersedes:
            f.write(f"SUPERSEDES: {', '.join(supersedes)}\n")

    # Označit staré soubory viditelným deprecation markerem (bezpečný append na začátek)
    # Díky tomu QMD / agent při retrievalu starého textu hned vidí, že je zastaralý.
    # === Update session_index for intra-session supersedes tracking ===
    try:
        idx = load_session_index()
        idx[session_id] = {
            "last_archive_docid": filename,
            "last_processed_at": datetime.now().isoformat(),
            "projekt": result.get("projekt", "")
        }
        save_session_index(idx)
    except Exception as e:
        print(f"[dreamer] Warning: session_index update failed: {e}")

    for old_name in supersedes:
        old_path = ARCHIVE_DIR / old_name
        if old_path.exists() and old_name != filename:
            try:
                old_text = old_path.read_text(encoding="utf-8")
                marker = f"> **[ZASTARALÉ – SUPERSEDED by {filename}]** Novější session aktualizovala stav nebo opravila fakta. Nepoužívej tento záznam jako aktuální.\n\n"
                body = old_text
                if body.startswith("> **[ZASTARALÉ"):
                    body = re.sub(r"^> \*\*\[ZASTARALÉ.*?\n\n", "", body, count=1, flags=re.DOTALL)
                if body.startswith("---"):
                    body = patch_frontmatter(
                        body,
                        {
                            "superseded_by": filename,
                            "retrieval_tier": "superseded",
                        },
                    )
                old_path.write_text(marker + body, encoding="utf-8")
                print(f"   🔗 Označeno jako superseded: {old_name} → {filename}")
            except Exception as e:
                print(f"   ⚠️  Nepodařilo se označit {old_name}: {e}")

    print(f"   💾 {filepath}")
    return filename


def main():
    parser = argparse.ArgumentParser(description="Dreamer v2 — historický analytik")
    parser.add_argument("--new-only", action="store_true", help="Jen nové session")
    parser.add_argument("--session-id", type=str, help="Jedna konkrétní session")
    parser.add_argument("--limit", type=int, default=0, help="Max session ke zpracování (0=vše)")
    args = parser.parse_args()

    index = load_sessions_index()
    state = load_state()
    processed = state.get("processed", {})
    known_projects = sorted(set(load_catalog_slugs()) | set(load_prompt_projects()))
    catalog_block = format_catalog_for_prompt(load_project_catalog())

    model = active_model_id() or "mtplx@localhost:8085"
    print(f"🔍 Dreamer v2 | {len(index)} sessions v indexu | {len(known_projects)} kanonických projektů | {model}")
    print(f"   Archiv: {ARCHIVE_DIR}")
    print(f"   State: {STATE_FILE}")

    if args.new_only:
        print(f"   Režim: --new-only ({len(processed)} již zpracováno)")

    count = 0
    archived_count = 0
    archived_files: list[str] = []
    lim = args.limit or len(index)

    for session_key, meta in sorted(
        iter_session_meta(index),
        key=lambda kv: kv[1].get("updated_at") or "",
    ):
        if meta.get("platform") not in ("discord", "telegram"):
            continue

        session_id = meta.get("session_id")
        updated_at = meta.get("updated_at")
        if not session_id:
            continue
        if args.session_id and session_id != args.session_id:
            continue

        # Skip gate uses the CONTENT clock, not the index (group/channel) clock.
        # The index updated_at advances on ANY activity in the channel, so content
        # frozen in time was re-archived forever (re-archive loop). A static session
        # now archives exactly once; genuinely growing sessions still re-archive.
        # Hermes v0.21 removed JSON snapshots, so the content clock now comes from
        # state.db (MAX(messages.timestamp)); snapshot last_updated stays the
        # preferred source when the file still exists.
        content_updated = content_timestamp(session_id) or updated_at
        # Fingerprint of the last message — the real anti-loop gate. A bot/cron
        # reply advances MAX(timestamp) without changing the conversation, so a
        # timestamp-only gate re-archives frozen sessions forever.
        content_digest = message_digest(session_id)
        prev_digest = processed.get(f"{session_key}:digest")

        if args.new_only:
            # Digest gate: same last-message content → skip even if the channel
            # clock advanced (bot/cron posts rewriting the tail).
            if content_digest and prev_digest == content_digest:
                continue
            # Legacy entries (no digest stored yet): original timestamp gate.
            if prev_digest is None and processed.get(session_key) and processed.get(session_key) >= content_updated:
                continue

        if count >= lim:
            break

        count += 1
        display_name = meta.get("display_name", "") or "?"

        raw = load_session_content(session_id)
        if not raw:
            print(f"⚠️  {display_name[:50]} — obsah chybí (snapshot i state.db)")
            continue
        clean_text, session_start = clean_session(raw, max_chars=MAX_SESSION_CHARS, include_tools=True)

        if len(clean_text) < 100:
            print(f"⚠️  {display_name[:50]} — příliš krátká ({len(clean_text)} chars)")
            continue

        char_count = len(clean_text)
        tier = session_tier(char_count)
        print(f"\n📌 ({count}) {display_name[:60]}... ({char_count} chars, tier={tier.label})")

        # Supersedes candidates — project-focused for hot recurring topics (teacher, hermes-noemem...)
        candidates_list = find_candidate_memories_for_project("teacher") + find_candidate_memories_for_project("hermes-noemem")
        candidates_text = catalog_block
        if candidates_list:
            candidates_text += (
                "\nPŘEDCHOZÍ ZÁZNAMY PRO STEJNÉ PROJEKTY (zvážit SUPERSEDES pokud tato session nahrazuje starší stav/fakt):\n"
                + "\n".join(f"- {c}" for c in candidates_list[:4])
                + "\n"
            )

        output, run_meta = run_dreamer_for_session(
            clean_text,
            session_start,
            known_projects,
            candidates_text,
        )
        if run_meta.get("multipass"):
            print(f"  multipass_anchor_fidelity (facts={run_meta.get('pass1_facts', '?')})")
        else:
            print("  tiered_anchor_fidelity")
        if run_meta.get("verify_retry"):
            print(
                f"  ↻ verify retry "
                f"(missing anchors={len(run_meta.get('missing_anchors_after') or [])})"
            )
        if not output:
            print("  ⚠️  LLM neodpověděl, přeskočeno")
            continue

        parsed = parse_output(output)
        if not parsed:
            print("  ⚠️  Výstup se nepodařilo zpracovat")
            print(f"  Raw: {output[:200]}...")
            continue

        raw_projekt = parsed.get("projekt") or ""
        project, proj_reason = resolve_project(raw_projekt, clean_text or "")
        parsed["projekt"] = project
        if proj_reason not in ("catalog", "ok"):
            print(f"  🎯 projekt: {raw_projekt!r} → {project!r} ({proj_reason})")

        filename = save_archive(parsed, session_id, updated_at)
        archived_files.append(filename)
        archived_count += 1
        processed[session_key] = content_updated
        if content_digest:
            processed[f"{session_key}:digest"] = content_digest

        # Uložit state po každé session
        state["processed"] = processed
        state["last_run_at"] = datetime.now().isoformat()
        save_state(state)

    print(f"\n🏁 Dreamer v2 dokončen — zpracováno {count} session, archivováno {archived_count}")
    if count == 0:
        print("   (žádné nové session k zpracování)")
    write_dreamer_run_log(archived_count=archived_count, archived_files=archived_files)


def write_dreamer_run_log(*, archived_count: int, archived_files: list[str], status: str = "ok", error: str = ""):
    """Jednoduchý log pro healthcheck (~/hermes-noemem/logs/dreamer-YYYY-MM-DD.log)."""
    try:
        from datetime import date

        log_dir = NOEMEM_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"dreamer-{date.today().isoformat()}.log"
        with open(log_file, "w", encoding="utf-8") as lf:
            lf.write(f"Dreamer v2 — {date.today().isoformat()}\n")
            lf.write(f"status: {status}\n")
            lf.write(f"archived_sessions: {archived_count}\n")
            if error:
                lf.write(f"error: {error}\n")
            lf.write("archived_files:\n")
            for name in archived_files:
                lf.write(f"- {name}\n")
            if not archived_files:
                lf.write("(none)\n")
        print(f"📝 Dreamer log: {log_file.name}")
    except Exception as e:
        print(f"⚠️  Failed to write dreamer log: {e}")


def collect_project_signals(projekt: str, max_count: int = 5, max_chars: int = 3500) -> str:
    """Levně sesbírá recentní archivní záznamy pro daný projekt (stejný match jako supersedes).
    Vrací spojené texty (frontmatter + TÉMA + začátek VÝSLEDEK) pro LLM prompt.
    """
    if not projekt or projekt.upper().startswith("NOV"):
        return ""
    parts = []
    total = 0
    try:
        for path in sorted(ARCHIVE_DIR.glob("*.md"), reverse=True)[:100]:
            if path.name.startswith("_"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")[:1200]
                if f"project: {projekt}" in text or projekt.lower() in path.name.lower():
                    parts.append(f"=== {path.name} ===\n{text}\n")
                    total += len(text) + 20
                    if len(parts) >= max_count or total >= max_chars:
                        break
            except Exception:
                pass
    except Exception:
        pass
    return "\n".join(parts)


def get_ground_truth_probe(projekt: str, max_chars: int = 1600) -> str:
    """Levný disk probe z ~/Projekty/ (ground truth, např. přesný stav pipeline v teacher.md).
    Hledá sekci se "Stav" nebo bere konec souboru.
    """
    cands = [PROJEKTY_DIR / f"{projekt}.md"]
    if projekt.lower() in ("hermes-noemem", "hermes"):
        cands.append(PROJEKTY_DIR / "hermes-noemem.md")
    if projekt.lower() in ("mtplx", "mlx-vlm"):
        cands.append(PROJEKTY_DIR / "mtplx-testing-notes.md")
    if projekt.lower() in ("qmd",):
        cands.append(PROJEKTY_DIR / "qmd-memory-plugin.md")
    for cand in cands:
        if cand and cand.exists():
            try:
                text = cand.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r'(##+\s*(Stav|Status|Pipeline|Aktuální|Current).*?(?=\n## |\n# |\Z))', text, re.DOTALL | re.IGNORECASE)
                if m:
                    excerpt = m.group(1).strip()[:max_chars]
                else:
                    excerpt = text[-max_chars:] if len(text) > max_chars else text
                return f"PROJEKTY/{cand.name}:\n{excerpt}\n"
            except Exception:
                pass
    return ""


def get_teacher_facts() -> str:
    """Přímý faktický stav z disk ground truth (registry topics/*.json + existence assembled_file v assemblies/).

    Žádný LLM. Používá stejnou logiku jako watchdog (existence souboru = hotovo).
    Vrací seznam předmětů ve stylu teacher.md pro přímé vložení do current.
    """
    labels = {
        "matematika": "Matematika (1.–9. r.)",
        "dejepis": "Dějepis (6.–9. r.)",
        "zemepis": "Zeměpis (6.–9. r.)",
        "prirodopis": "Přírodopis (6.–9. r.)",
        "fyzika": "Fyzika (6.–9. r.)",
        "prvouka": "Prvouka (1.–3. ročník)",
        "prirodoveda": "Přírodověda (4.–5. ročník)",
        "vlastiveda": "Vlastivěda (4.–5. ročník)",
    }
    lines = []
    try:
        for jf in sorted(TEACHER_TOPICS.glob("*.json")):
            if ".bak" in jf.name:
                continue
            p = jf.stem
            try:
                topics = json.loads(jf.read_text(encoding="utf-8", errors="ignore"))
                if not isinstance(topics, list):
                    continue
                target = len(topics)
                done = 0
                for t in topics:
                    fn = t.get("assembled_file") if isinstance(t, dict) else None
                    if fn and (TEACHER_ASSEMBLIES / p / fn).exists():
                        done += 1
                label = labels.get(p, p.capitalize())
                if done >= target:
                    status = f"✅ HOTOVO {done}/{target}"
                else:
                    status = f"🏗️ BĚŽÍ {done}/{target}"
                lines.append(f"*   **{label}** {status}")
            except Exception:
                pass
    except Exception:
        pass
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def refresh_current_status(projects: list[str] = None):
    """Syntetizuje a zapíše living current status pro horké/recurring projekty.

    Běží před qmd update+embed (viz nightly wrapper).
    - Pro "teacher": čistě kódové fakta z disk ground truth (topics reg + assemblies existence). Bez LLM pro counts/stav.
    - Pro ostatní: levné probes + malý LLM (max_tokens omezeno).
    - Sbírá recentní archive pro "poslední aktivity".
    - Vytváří {slug}-current.md v ~/hermes-noemem/projekty/ (viditelné pro QMD).
    - Názvy: teacher-current.md + ostatní dle častých projektů.

    Non-fatal — volá se s || true z wrapperu.
    """
    if projects is None or len(projects) == 0:
        projects = ["teacher", "hermes-noemem", "xnotes", "mtplx", "mlx-vlm", "qmd", "hermes"]

    PROJEKTY_CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n📈 refresh_current_status → {len(projects)} projects | target: {PROJEKTY_CURRENT_DIR}")

    for projekt in projects:
        try:
            signals = collect_project_signals(projekt)
            probe = get_ground_truth_probe(projekt)
            if not signals and not probe:
                print(f"   ⏭️  {projekt}: žádné signály ani probe, přeskočeno")
                continue

            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d %H:%M")

            if projekt == "teacher":
                # === Teacher special path: faktický, bez LLM (nebo minimálně) ===
                # Disk ground truth (registry + assembled files) je pravda, Projekty/teacher.md i staré signály mohou lagovat.
                facts = get_teacher_facts()
                if not facts:
                    print(f"   ⏭️  {projekt}: žádné facts z disk ground truth, přeskočeno")
                    continue

                # Jednoduché recent changes z archivních signálů (bez dalšího LLM)
                changes = []
                for m in re.finditer(r'TÉMA:\s*(.+)', signals or ""):
                    t = m.group(1).strip()[:90]
                    if t:
                        changes.append(f"*   {t}")
                changes_block = "\n".join(changes[:4]) if changes else "(žádné nové signály v archivu)"

                body = f"""# teacher — aktuální stav (k {date_str})

**Předměty & Pipeline**

{facts}
**Poslední aktivity (z recent archive)**

{changes_block}

> Fakta přímo z disk ground truth (topics/*.json + assemblies/ assembled_file existence). Žádný LLM pro počty/stav.
"""
                slug = slugify(projekt)
                fname = f"{slug}-current.md"
                fpath = PROJEKTY_CURRENT_DIR / fname

                header = f"""---
project: {projekt}
updated: {now.isoformat()}
category: projects
source: dreamer_v2.refresh_current_status
---

"""
                footer = f"\n\n> Automaticky obnoveno {now.isoformat()} (refresh_current_status, hermes-noemem; code facts)."

                fpath.write_text(header + body + footer, encoding="utf-8")
                print(f"   ✅ {fname} (code facts from assemblies+registry, no LLM)")
                continue

            # === Standard path for other projects (still uses small LLM synth) ===
            prompt = f"""Jsi aktualizátor living current status v hermes-noemem.

Na základě signálů (nedávné archivní paměti + ground-truth excerpt z ~/Projekty/) vytvoř KRÁTKÝ, přesný a užitečný aktuální stav pro projekt "{projekt}".

Pravidla:
- Používej výhradně fakta ze signálů — nic nevymýšlej a nehalucinuj.
- Výstup jako čistý markdown (žádné code blocky), začni rovnou # Nadpis.
- Styl: pro teacher/hermes projekty preferuj formát jako v teacher.md — "Prvouka (1.–3. ročník, 81 témat) ✅ HOTOVO 81/81", "Fyzika ... 🏗️ BĚŽÍ 15/133", otevřené věci, poslední změny.
- Celkem výstup max ~900 znaků. Stručné odrážky nebo 2–4 krátké sekce.
- Na konci uveď aktuální datum {date_str}.

SIGNÁLY Z ARCHIVU (recent memories pro projekt):
{signals or "(žádné)"}

GROUND TRUTH / PROBE Z ~/Projekty/:
{probe or "(žádné)"}

Výstup (přímo markdown):"""

            output = call_llm(prompt, max_tokens=500, enable_thinking=False)
            if not output or len(output.strip()) < 15:
                print(f"   ⚠️  {projekt}: LLM nevrátil smysluplný výstup, přeskočeno")
                continue

            slug = slugify(projekt)
            fname = f"{slug}-current.md"
            fpath = PROJEKTY_CURRENT_DIR / fname

            header = f"""---
project: {projekt}
updated: {now.isoformat()}
category: projects
source: dreamer_v2.refresh_current_status
---

"""
            body = f"# {projekt} — aktuální stav (k {date_str})\n\n{output.strip()}\n"
            footer = f"\n\n> Automaticky obnoveno {now.isoformat()} (refresh_current_status, hermes-noemem)."

            fpath.write_text(header + body + footer, encoding="utf-8")
            print(f"   ✅ {fname} (LLM {len(output)} znaků)")
        except Exception as e:
            print(f"   ❌ {projekt}: {e}")

    print("📈 refresh_current_status dokončen")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            write_dreamer_run_log(archived_count=0, archived_files=[], status="error", error=str(e))
        except Exception:
            pass
        raise
