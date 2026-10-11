#!/usr/bin/env python3
"""Normalize Hermes session JSON into LLM-readable text.

Tool results carry paths, command output, and file snippets. Older cleaners
dropped them entirely, which made Dreamer infer facts from assistant prose.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional

DEFAULT_MAX_SESSION_CHARS = 14000
USER_CLIP = 800
ASSISTANT_CLIP = 750
TOOL_CLIP = 1200
MAX_SUBSTANTIVE_TOOLS = 80

_NOISE_TOOL_EXACT = {
    '{"total_count": 0}',
    '{"result": ""}',
}
_SUBSTANTIVE_MARKERS = (
    "/users/",
    "/.hermes",
    "projekty",
    "output",
    "stderr",
    "stdout",
    "exit code",
    "command:",
    "error",
    "traceback",
    "file:",
    ".py",
    ".sh",
    ".md",
    ".plist",
    "mtplx",
    "launchctl",
    "brew ",
    "hunspell",
    "content",
    "files",
    "path",
)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _tool_is_noise(content: str) -> bool:
    s = content.strip()
    if not s or len(s) < 20:
        return True
    if s in _NOISE_TOOL_EXACT:
        return True
    if '"count": 0' in s and '"results": []' in s:
        return True
    if '"mode": "discover"' in s and '"count": 0' in s:
        return True
    if s.startswith('{"total_count": 0'):
        return True
    return False


def _tool_is_substantive(content: str) -> bool:
    if _tool_is_noise(content):
        return False
    lower = content.lower()
    if any(marker in lower for marker in _SUBSTANTIVE_MARKERS):
        return True
    return len(content) >= 180


def _tool_label(msg: dict) -> str:
    name = (msg.get("name") or msg.get("tool_call_id") or "result").strip()
    return f"TOOL[{name}]"


def _should_skip_user(content: str) -> bool:
    return (
        "Review the conversation above and update the skill library" in content
        or "maximum number of tool-calling iterations" in content
    )


def _fit_session_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 3
    tail = max_chars - head - 80
    return (
        text[:head].rstrip()
        + "\n\n... (střed session ořezán — zachován začátek a závěr) ...\n\n"
        + text[-tail:].lstrip()
    )


def clean_session(
    raw: dict,
    *,
    max_chars: int = DEFAULT_MAX_SESSION_CHARS,
    include_tools: bool = True,
) -> tuple[str, str]:
    """Return (clean_text, session_start)."""
    title = raw.get("display_name") or raw.get("title") or "Untitled"
    session_start = raw.get("session_start") or datetime.now().isoformat()
    lines = [f"# {title}", f"Platform: {raw.get('platform', '?')} | {session_start[:19]}\n"]

    messages = raw.get("messages", [])
    if not isinstance(messages, list):
        text = json.dumps(raw, ensure_ascii=False)
        pairs = re.findall(
            r'"role"\s*:\s*"(user|assistant)".*?"content"\s*:\s*"((?:[^"\\]|\\.)*)"',
            text,
            re.DOTALL,
        )
        for role, content in pairs:
            if len(content.strip()) > 20:
                icon = "USER" if role == "user" else "ASSISTANT"
                lines.append(f"{icon}: {_clip(content, ASSISTANT_CLIP)}")
        result = "\n\n".join(lines)
        return _fit_session_text(result, max_chars), session_start

    substantive_tools = 0
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role in ("system", "function"):
            continue
        if role == "tool":
            if not include_tools:
                continue
            if not content or not isinstance(content, str):
                continue
            if not _tool_is_substantive(content):
                continue
            if substantive_tools >= MAX_SUBSTANTIVE_TOOLS:
                continue
            substantive_tools += 1
            lines.append(f"{_tool_label(msg)}: {_clip(content, TOOL_CLIP)}")
            continue
        if not content or not isinstance(content, str) or len(content.strip()) < 15:
            continue
        if role == "user" and _should_skip_user(content):
            continue
        icon = "USER" if role == "user" else "ASSISTANT"
        clip = USER_CLIP if role == "user" else ASSISTANT_CLIP
        lines.append(f"{icon}: {_clip(content, clip)}")

    result = "\n\n".join(lines)
    return _fit_session_text(result, max_chars), session_start