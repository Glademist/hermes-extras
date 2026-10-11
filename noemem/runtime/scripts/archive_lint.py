#!/usr/bin/env python3
"""archive_lint.py — pure-code quality checks for Dreamer archives (no LLM).

Checks (error unless noted):
  L1 truncated backtick content: unclosed backtick, or path ending mid-segment
  L2 literal \\n sequence inside backticks
  L3 empty VÝSLEDEK or PONAUČENÍ (skipped for superseded records)
  L4 project: empty or not in project catalog  (WARNING only)
  L5 frontmatter missing/unparseable or missing required keys
  L6 wrong-case path, e.g. /users/you/... instead of ~/...
     (skipped for superseded records)

Usage:
  python3 archive_lint.py --all            # baseline over whole archive/
  python3 archive_lint.py --file PATH      # single file; exit 0 = clean, 1 = errors
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR.parent))
sys.path.insert(0, str(_SCRIPTS_DIR))
from noemem_paths import ARCHIVE_DIR, NOEMEM_ROOT  # noqa: E402

CATALOG = NOEMEM_ROOT / "personal" / "project_catalog.md"
LOG_DIR = NOEMEM_ROOT / "logs"

REQUIRED_FM_KEYS = ("created", "source_session_id", "category")


def parse_frontmatter(text: str):
    m = re.search(r"^---\s*\n(.*?)\n---", text, re.DOTALL | re.MULTILINE)
    if not m:
        return None
    meta = {}
    for line in m.group(1).split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip()
    return meta


def load_catalog_slugs() -> set:
    slugs = set()
    if CATALOG.exists():
        for m in re.finditer(r"^([a-z0-9][a-z0-9_-]{2,}):", CATALOG.read_text(encoding="utf-8"), re.M):
            slugs.add(m.group(1).lower())
    return slugs


def field_value(text: str, key: str) -> str:
    # `[ \t]*`, never `\s*`: with re.MULTILINE, `\s*` also matches the newline, so
    # an EMPTY field captured the NEXT line instead of "" — `TÉMA: ` (empty) read
    # back as "PROČ: " and passed the L3 non-empty test. That silently disabled
    # the empty-field check for every field it was applied to (2026-10-01).
    m = re.search(rf"^{key}:[ \t]*(.*)$", text, re.MULTILINE)
    return (m.group(1).strip() if m else "")


# Path that ends suspiciously mid-segment: last component shorter than 4 chars
# and not a known-good short name/extension.
_OK_SHORT_TAILS = {"md", "py", "sh", "js", "ts", "log", "json", "yaml", "yml", "txt", "db", "bak", "lib", "bin", "etc", "opt", "usr", "var", "tmp", "ai"}


def truncated_path(s: str) -> bool:
    # only judge real filesystem paths, not units like "26 tok/s"
    if not s.startswith(("/", "~/")):
        return False
    if "/" not in s:
        return False
    tail = s.rstrip("/").split("/")[-1]
    last = tail.split(".")[-1].lower()
    if last in _OK_SHORT_TAILS:
        return False
    # ends with dot or hyphen, or final extensionless component < 4 chars
    if tail.endswith((".", "-", "_")):
        return True
    if "." not in tail and len(tail) < 4:
        return True
    return False


def path_exists(s: str) -> bool:
    """True when the flagged path resolves on this machine — then it is NOT truncated.

    A 2026-09-22 audit found real, existing paths quarantined by L1 (`~/.local/bin/hf`,
    `~/models/mlx/`). If the path exists, the record was factually right and the rule
    was wrong. Unresolvable paths (other-machine `/root/...`) stay flagged: conservative.
    """
    from glob import iglob

    if "*" in s or "?" in s:
        # glob pattern: exists if it matches anything
        return any(iglob(s))
    try:
        return Path(s).expanduser().exists()
    except (OSError, RuntimeError):
        return False


# A literal \n inside backticks is corruption ONLY when it is dreamer garbage
# (literal "\n" replacing a real newline in prose). A shell format string such as
# `curl -w '%{http_code}\n'` is a correctly quoted command that was really run —
# quarantining it on 2026-09-15 hid a factually complete record from consolidation.
_SHELL_FORMAT = re.compile(r"%\{|^\s*(curl|printf|echo)\b")

# L6 — path casing. A path written with wrong case is unusable off this Mac
# (APFS is case-insensitive, so `path_exists()` cannot catch it) and it is the
# signature of a lowercased anchor being copied into the archive.
#
# Only the ~ tree is checked: those segments have unambiguous
# on-disk casing, and the lookbehind keeps `~/...`, bare `/Projekty/...` and
# `/v1/...` out of scope.
_WRONG_CASE = re.compile(
    r"(?<![\w~/.-])(?P<path>/(?:users/you|Users/you)/[^\s`\"')]*)"
)


def _wrong_case_paths(text: str) -> list[str]:
    """Return home-relative paths whose first segments are not ~."""
    out: list[str] = []
    for m in _WRONG_CASE.finditer(text):
        tok = m.group("path")
        if not tok.startswith("~"):
            out.append(tok)
    return out


# L7 — category/personal must not be used to park infrastructure. On 2026-09-30
# memory_seed.py routed category=personal into user-profile.md; the enum is 3/4
# inert (FALLBACK_BY_CATEGORY has one key), so any infra session that landed on
# `personal` by default poisoned the user profile with ports, launchd plists and
# model inventory — including a stale "Darkbloom available" claim. Infra belongs
# in infra.md / hermes-system.md. These markers only fire on personal records.
_INFRA_MARKERS = (
    "launchd", "launchctl", "plist", "127.0.0.1", "localhost:",
    "cron", ":8096", ":8098", ":8085", ":8082", ":8026", ":8181", ":8888", ":9377",
    "darkbloom", "mtplx", "mlx", "deepseek", "qwen", "gemma", "launchagent",
    "port", "llama-server", "mlx_lm", "oMLX", "model inventory",
)


def _infra_in_personal(text: str) -> list[str]:
    """Infrastructure markers found in a record classified as `personal`."""
    low = text.lower()
    return [m for m in _INFRA_MARKERS if m.lower() in low]


def lint_file(path: Path, catalog: set) -> dict:
    issues = []
    warnings = []
    text = path.read_text(encoding="utf-8", errors="replace")

    fm = parse_frontmatter(text)
    if fm is None:
        issues.append("L5: frontmatter missing or unparseable")
        fm = {}
    else:
        missing = [k for k in REQUIRED_FM_KEYS if not fm.get(k)]
        if missing:
            issues.append(f"L5: frontmatter missing keys: {', '.join(missing)}")

    superseded = fm.get("retrieval_tier") == "superseded"

    # L1/L2 — backtick integrity
    if text.count("`") % 2 == 1:
        issues.append("L1: odd number of backticks (unclosed inline code)")
    for m in re.finditer(r"`([^`]+)`", text):
        content = m.group(1)
        # L2: skip deliberate shell format strings (curl -w '%{...}\n', printf, echo)
        if "\\n" in content and not _SHELL_FORMAT.search(content):
            issues.append(f"L2: literal \\n inside backticks: `{content[:60]}`")
        # L1: a path that resolves on this machine is not truncated
        if truncated_path(content) and not path_exists(content):
            issues.append(f"L1: suspicious truncated path: `{content[:80]}`")

    # bare truncated paths in prose (outside backticks): line ends mid-path
    for m in re.finditer(r"(/Users/[^\s`\"']+)$", text, re.MULTILINE):
        if truncated_path(m.group(1)):
            issues.append(f"L1: line ends mid-path: {m.group(1)[:80]}")

    # L3 — empty fields (live records only)
    # TÉMA and PROČ belong here too. A record whose TÉMA is empty gets the
    # fallback filename `{project}-session.md`, carries no topic, and was still
    # being written with `retrieval_tier: live` — 6 such hollow records existed
    # (4 of them superseding a good record of the SAME session, so the good copy
    # was demoted to `superseded` and retrieval only ever saw the hollow one).
    if not superseded:
        for key in ("TÉMA", "PROČ", "VÝSLEDEK", "PONAUČENÍ"):
            if not field_value(text, key):
                issues.append(f"L3: empty {key}")

    # L6 — wrong-case paths (live records only; superseded are historical)
    if not superseded:
        for tok in _wrong_case_paths(text)[:3]:
            issues.append(f"L6: wrong-case path: {tok[:70]}")

    # L7 — infrastructure parked under category=personal (see _INFRA_MARKERS).
    # Warning, not error: a personal record may legitimately mention a tool once.
    if not superseded and (fm.get("category") or "").strip().lower() == "personal":
        hits = _infra_in_personal(text)
        if len(hits) >= 3:
            warnings.append(
                f"L7: category=personal but infra content ({', '.join(hits[:5])}) "
                "— belongs in infra.md / hermes-system.md, not user-profile.md"
            )

    # L4 — project sanity (warning)
    proj = (fm.get("project") or field_value(text, "PROJEKT")).lower()
    if not superseded:
        if not proj:
            warnings.append("L4: empty project")
        elif catalog and proj not in catalog:
            warnings.append(f"L4: project '{proj}' not in catalog")

    return {
        "file": path.name,
        "superseded": superseded,
        "errors": issues,
        "warnings": warnings,
    }


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true")
    g.add_argument("--file", type=str)
    ap.add_argument("--json-log", action="store_true", help="write logs/archive-lint-DATE.json (default with --all)")
    args = ap.parse_args()

    catalog = load_catalog_slugs()
    files = sorted(ARCHIVE_DIR.glob("*.md")) if args.all else [Path(args.file)]
    files = [f for f in files if not f.name.startswith("_")]

    results = [lint_file(f, catalog) for f in files]
    bad = [r for r in results if r["errors"]]
    warned = [r for r in results if r["warnings"] and not r["errors"]]

    for r in results:
        if r["errors"] or r["warnings"]:
            tag = "SUPERSEDED" if r["superseded"] else "LIVE"
            print(f"\n✗ {r['file']} [{tag}]")
            for e in r["errors"]:
                print(f"   ERROR {e}")
            for w in r["warnings"]:
                print(f"   warn  {w}")

    print(f"\n=== {len(files)} files | {len(bad)} with errors | {len(warned)} warnings-only | {len(files)-len(bad)-len(warned)} clean ===")

    if args.all or args.json_log:
        LOG_DIR.mkdir(exist_ok=True)
        out = LOG_DIR / f"archive-lint-{date.today().isoformat()}.json"
        out.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"log: {out}")

    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
