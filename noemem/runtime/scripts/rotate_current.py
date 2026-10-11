#!/usr/bin/env python3
"""Weekly rotation for {key}-current.md (hyphen convention, krok 6)."""

import sys
from datetime import datetime
from pathlib import Path


def rotate_current(key: str, base_dir: Path) -> Path:
    """Archive {key}-current.md to weekly/{key}_YYYY-Www.md; touch fresh -current."""
    base_dir = Path(base_dir).expanduser().resolve()
    weekly = base_dir / "weekly"
    weekly.mkdir(parents=True, exist_ok=True)

    current = base_dir / f"{key}-current.md"
    if current.exists() and current.stat().st_size > 0:
        week = datetime.now().strftime("%Y-W%W")
        archive = weekly / f"{key}_{week}.md"
        if archive.exists():
            archive = weekly / f"{key}_{week}_{datetime.now().strftime('%H%M')}.md"
        current.rename(archive)
        print(f"  archived → {archive.name}")

    current.write_text("", encoding="utf-8")
    return current


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: rotate_current.py <key> <base_dir>")
        sys.exit(1)

    key = sys.argv[1]
    base = Path(sys.argv[2])
    new_current = rotate_current(key, base)
    print(f"Rotated → {new_current}")