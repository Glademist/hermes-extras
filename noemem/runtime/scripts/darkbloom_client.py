#!/usr/bin/env python3
"""darkbloom_client.py — OpenAI-compatible client for Darkbloom (self-route).

Drop-in backend for mtplx_client router when LLM_BACKEND=darkbloom.

Mirrors ExampleProject usage:
  base_url: https://api.darkbloom.dev/v1
  header:   X-Darkbloom-Route: self   (free inference on this Mac)
  model:    gemma-4-26b (override via DARKBLOOM_MODEL)

Auth key resolution order:
  1. DARKBLOOM_API_KEY env
  2. file at DARKBLOOM_API_KEY_FILE (default ~/.config/darkbloom/api_key)
  3. llm.api_key from ExampleProject config.yaml (same key used by booksort)

Reasoning / thinking:
  gemma-4-26b accepts enable_thinking but does NOT emit reasoning /
  reasoning_content fields (verified 2026-07-17). Visible answer is always
  in message.content. Flag is still forwarded for API compatibility.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import requests

DEFAULT_BASE_URL = "https://api.darkbloom.dev/v1"
DEFAULT_MODEL = "gemma-4-26b"
ROUTE_HEADER = "X-Darkbloom-Route"
ROUTE_VALUE = "self"
DEFAULT_KEY_FILE = Path.home() / ".config" / "darkbloom" / "api_key"
XNOTES_CONFIG = Path("~/AI/ExampleProject/config.yaml")


def _read_key_file(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    # allow "KEY=..." or bare key; take first non-empty non-comment line
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and not line.startswith("sk-"):
            line = line.split("=", 1)[1].strip().strip('"').strip("'")
        return line
    return None


def _read_xnotes_api_key() -> Optional[str]:
    try:
        text = XNOTES_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'api_key:\s*["\']([^"\']+)["\']', text)
    if not m:
        return None
    key = m.group(1).strip()
    return key or None


def _resolve_api_key() -> str:
    env = (os.environ.get("DARKBLOOM_API_KEY") or "").strip()
    if env:
        return env
    key_file = Path(
        os.environ.get("DARKBLOOM_API_KEY_FILE") or DEFAULT_KEY_FILE
    ).expanduser()
    from_file = _read_key_file(key_file)
    if from_file:
        return from_file
    from_xnotes = _read_xnotes_api_key()
    if from_xnotes:
        return from_xnotes
    raise RuntimeError(
        "Darkbloom API key not found. Set DARKBLOOM_API_KEY, or put the key in "
        f"{DEFAULT_KEY_FILE}, or keep llm.api_key in {XNOTES_CONFIG}."
    )


def base_url() -> str:
    return os.environ.get("DARKBLOOM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def chat_completions_url() -> str:
    return f"{base_url()}/chat/completions"


def health_url() -> str:
    # coordinator has no dedicated health; models list is a cheap probe
    return f"{base_url()}/models"


@lru_cache(maxsize=1)
def active_model_id() -> str:
    return os.environ.get("DARKBLOOM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def parse_assistant_content(message: dict[str, Any]) -> str:
    """Return visible assistant text.

    Darkbloom/Gemma: content only. Still tolerate MTPLX/Nous-style fields
    if a different model is routed later.
    """
    content = str(message.get("content") or "").strip()
    if content:
        return content
    for key in ("reasoning", "reasoning_content"):
        reasoning = str(message.get(key) or "").strip()
        if not reasoning:
            continue
        if "</think>" in reasoning:
            return reasoning.split("</think>", 1)[-1].strip()
        if " response" in reasoning:
            return reasoning.split(" response", 1)[-1].strip()
    return ""


def prepare_chat_body(body: dict[str, Any]) -> dict[str, Any]:
    """Ensure model is set; keep enable_thinking if present (no-op for Gemma)."""
    out = dict(body)
    if not out.get("model"):
        out["model"] = active_model_id()

    reasoning = out.pop("reasoning", None)
    if "enable_thinking" not in out and isinstance(reasoning, dict):
        if "enabled" in reasoning:
            out["enable_thinking"] = bool(reasoning["enabled"])
    elif isinstance(reasoning, bool):
        out["enable_thinking"] = reasoning
    return out


def post_chat(
    body: dict[str, Any],
    *,
    timeout: int = 900,
    client_controls: bool | None = None,
) -> requests.Response:
    """POST to Darkbloom /chat/completions with self-route header."""
    del client_controls  # MTPLX-only; unused on Darkbloom
    prepared = prepare_chat_body(body)
    token = _resolve_api_key()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        ROUTE_HEADER: ROUTE_VALUE,
    }
    return requests.post(
        chat_completions_url(),
        json=prepared,
        headers=headers,
        timeout=timeout,
    )
