"""Prompt-Guard configuration — thresholds, tool targets, paths."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

# --- Classification thresholds ---

# Default indirect injection threshold (web content, search results, browser)
# Scores above this trigger a warning.
# Note: the Prompt-Guard-86M model is extremely sensitive on English text
# (high false positive rate out-of-box). The pattern fallback has near-zero FP.
# With the model backend, raise threshold to 0.95 unless fine-tuned.
DEFAULT_INDIRECT_THRESHOLD = 0.7

# Default jailbreak threshold (user prompts / dialogue)
DEFAULT_JAILBREAK_THRESHOLD = 0.7

# Temperature for softmax scaling (1.0 = standard). Higher = less confident outputs.
SOFTMAX_TEMPERATURE = 1.0

# --- Model ---

MODEL_ID = "meta-llama/Prompt-Guard-86M"

# HF cache dir (matches ~/.zshenv)
HF_HOME = Path(os.environ.get("HF_HOME", "~/models/.hf_cache")).expanduser()

# Local model path (snapshot_download target)
MODEL_PATH = HF_HOME / "hub" / f"models--{MODEL_ID.replace('/', '--')}"

# --- Tool targets for the Hermes plugin ---

# Tools that import external content — these get scanned for indirect injections.
EXTERNAL_TOOLS = frozenset({
    "web_extract",
    "web_search",
    "browser_navigate",
    "browser_snapshot",
    "browser_vision",
    "x_search",
})

# Max content length to scan (characters). Above this, scan only head + tail.
MAX_SCAN_CHARS = 50_000

# Chunk size for classification (DeBERTa context = 512 tokens, ~2000 chars)
CHUNK_SIZE = 2000

# --- Token resolution ---

# Known locations for HF token on this system
_TOKEN_SOURCES = [
    ("env", lambda: os.environ.get("HUGGING_FACE_HUB_TOKEN")),
    ("env", lambda: os.environ.get("HF_TOKEN")),
    ("file", Path.home() / ".env.local"),
    ("file", Path.home() / ".zshenv"),
    ("file", Path.home() / ".zshrc"),
]


def resolve_hf_token() -> Optional[str]:
    """Try to find the HF token from env vars or shell config files."""
    for source_type, source in _TOKEN_SOURCES:
        if source_type == "env":
            val = source()  # type: ignore[operator]
            if val and len(val) > 10 and val.startswith("hf_"):
                return val
        elif source_type == "file" and isinstance(source, Path):
            if not source.exists():
                continue
            try:
                text = source.read_text()
                # Match both HUGGING_FACE_HUB_TOKEN and HF_TOKEN exports
                m = re.search(
                    r'(?:export\s+)?(?:HUGGING_FACE_HUB_TOKEN|HF_TOKEN)\s*[=:]\s*["\']?(hf_[a-zA-Z0-9]+)["\']?',
                    text,
                )
                if m:
                    return m.group(1)
            except (OSError, PermissionError):
                continue
    return None
