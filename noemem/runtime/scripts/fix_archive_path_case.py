#!/usr/bin/env python3
"""fix_archive_path_case.py — case-only repair of ~ paths in archives.

Resolves each wrong-case path against the real filesystem so sub-segments
(AI, ExampleProject, Projekty) are restored too, not just the /users/you prefix.
Paths that do not resolve on disk get the prefix fix only, and are reported.

Read-only in --dry-run mode. Writes a dated backup before any mutation.

Usage:
  python3 fix_archive_path_case.py --dry-run
  python3 fix_archive_path_case.py --apply
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS.parent))
sys.path.insert(0, str(_SCRIPTS))

from archive_lint import _wrong_case_paths  # noqa: E402
from noemem_paths import ARCHIVE_DIR, NOEMEM_ROOT  # noqa: E402

HOME = Path.home()
BACKUP_ROOT = NOEMEM_ROOT.parent / "noemem-backups"


def true_casing(rel: str) -> str | None:
    """Restore each segment's real on-disk case, walking down from HOME.

    Deliberately does NOT use Path.resolve(): that follows symlinks, and
    `~/.venvs/mlx/bin/python` is a symlink into the uv python tree — resolving
    it would rewrite the archive to a different path, not a different casing.
    Only the deepest EXISTING ancestor is corrected; deeper segments that do
    not exist are kept verbatim.
    """
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return None
    # find how deep the chain actually exists
    depth = 0
    for i in range(len(parts)):
        probe = HOME.joinpath(*parts[: i + 1])
        if not probe.exists():
            break
        depth = i + 1
    if depth == 0:
        return None
    out: list[str] = []
    for i, seg in enumerate(parts):
        if i >= depth:
            out.append(seg)  # does not exist on disk; nothing to correct
            continue
        parent = HOME.joinpath(*out) if out else HOME
        try:
            real = next((e.name for e in parent.iterdir() if e.name.lower() == seg.lower()), None)
        except OSError:
            real = None
        out.append(real or seg)
    return "/".join(out)


def repair_token(tok: str) -> tuple[str, str]:
    """Return (repaired_token, how): 'disk' when resolved on disk, else 'prefix'."""
    # Path() drops a trailing slash, so a directory reference would lose it
    # and shorten the token — keep it explicitly.
    trailing = "/" if tok.endswith("/") else ""
    # tok is "/users/you/<rel>" — strip the home prefix before resolving,
    # otherwise the candidate path double-prefixes and never exists.
    rel = tok.rstrip("/").lstrip("/")
    for home_prefix in ("users/you/", "Users/you/"):
        if rel.lower().startswith(home_prefix.lower()):
            rel = rel[len(home_prefix):]
            break
    try:
        candidate = HOME / rel
        if candidate.exists():
            real = true_casing(rel)
            if real:
                return str(HOME / real) + trailing, "disk"
    except (OSError, ValueError):
        pass
    # fallback: only the /users/you prefix is unambiguously wrong
    if tok.lower().startswith("/users/you"):
        return "~" + tok[len("/users/you"):], "prefix"
    return tok, "none"


def replace_tokens(text: str, tokens: list[str]) -> str:
    """Longest token first, so a path is never partially rewritten by its prefix."""
    out = text
    for tok in sorted(set(tokens), key=len, reverse=True):
        fixed, how = repair_token(tok)
        if how != "none" and fixed != tok:
            out = out.replace(tok, fixed)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if not (args.dry_run or args.apply):
        ap.error("pass --dry-run or --apply")

    files = sorted(ARCHIVE_DIR.glob("*.md"))
    plan: list[dict] = []
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        if "retrieval_tier: superseded" in text:
            continue
        hits = _wrong_case_paths(text)
        if not hits:
            continue
        new_text = replace_tokens(text, hits)
        if new_text == text:
            continue
        # sanity: only case may differ
        assert new_text.lower() == text.lower(), f"non-case change in {p.name}"
        hows: dict[str, int] = {}
        for tok in set(hits):
            hows[repair_token(tok)[1]] = hows.get(repair_token(tok)[1], 0) + 1
        plan.append({
            "file": p.name,
            "paths": len(set(hits)),
            "how": hows,
            "bytes": len(text.encode()),
        })

    total_paths = sum(x["paths"] for x in plan)
    disk = sum(x["how"].get("disk", 0) for x in plan)
    prefix = sum(x["how"].get("prefix", 0) for x in plan)
    unres = sum(x["how"].get("none", 0) for x in plan)

    print("=" * 66)
    print("DRY RUN — case-only repair" if args.dry_run else "APPLY")
    print("=" * 66)
    print(f"files to change      : {len(plan)}")
    print(f"path tokens to fix   : {total_paths}")
    print(f"  resolved on disk   : {disk}")
    print(f"  prefix-only fix    : {prefix}")
    print(f"  unresolvable       : {unres}")
    print()
    for x in plan[:12]:
        print(f"  {x['file'][:56]:<58} {x['paths']:>3} paths  {x['how']}")
    if len(plan) > 12:
        print(f"  … +{len(plan) - 12} more files")

    if args.dry_run:
        print("\nNothing written. Re-run with --apply to execute.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_ROOT / f"archive-pathcase-{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    for x in plan:
        src = ARCHIVE_DIR / x["file"]
        shutil.copy2(src, backup / x["file"])
    (backup / "_manifest.json").write_text(
        json.dumps({"stamp": stamp, "files": [x["file"] for x in plan]}, indent=2),
        encoding="utf-8",
    )
    print(f"\nbackup -> {backup}")

    changed = 0
    for x in plan:
        p = ARCHIVE_DIR / x["file"]
        text = p.read_text(encoding="utf-8", errors="replace")
        new_text = replace_tokens(text, _wrong_case_paths(text))
        if new_text != text:
            assert new_text.lower() == text.lower(), f"non-case change in {p.name}"
            p.write_text(new_text, encoding="utf-8")
            changed += 1
    print(f"files rewritten      : {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
