#!/usr/bin/env python3
"""gemma_local_client.py — OpenAI-compatible client for local mlx_vlm server.

Drop-in backend for mtplx_client router when LLM_BACKEND=gemma_local.

Routes to the local mlx_vlm.server on :8096 (Gemma-4-E4B OptiQ-4bit + MTP
drafter, --enable-thinking). No auth. Used for the sandbox gemma re-run to
compare attribution against cloud deepseek — not the production path.

Model string must EXACTLY match the server's loaded model id (from /v1/models
or the LaunchAgent --model path). Default below matches the plist.

Thinking: mlx_vlm returns reasoning in message.reasoning_content (reasoning is
an identical duplicate). content can be None when thinking eats the whole
max_tokens budget → keep max_tokens generous (>=8192) for thinking roles.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import requests

DEFAULT_BASE_URL = os.environ.get("GEMMA_LOCAL_BASE_URL", "http://127.0.0.1:8096")
DEFAULT_MODEL = "~/models/mlx-lm-candidates/gemma-4-e4b-it-qat-OptiQ-4bit"


def base_url() -> str:
    return DEFAULT_BASE_URL.rstrip("/")


def chat_completions_url() -> str:
    return f"{base_url()}/v1/chat/completions"


@lru_cache(maxsize=1)
def active_model_id() -> str:
    return os.environ.get("GEMMA_LOCAL_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def parse_assistant_content(message: dict[str, Any]) -> str:
    """Return visible assistant text.

    mlx_vlm: content holds the answer; reasoning is in reasoning_content.
    Fall back to reasoning split on ' response' (mirrors nous_client).
    """
    content = str(message.get("content") or "").strip()
    if content:
        return content
    reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "").strip()
    if " response" in reasoning:
        return reasoning.split(" response", 1)[-1].strip()
    return ""


def prepare_chat_body(body: dict[str, Any]) -> dict[str, Any]:
    out = dict(body)
    if not out.get("model"):
        out["model"] = active_model_id()
    reasoning = out.pop("reasoning", None)
    if "enable_thinking" not in out and isinstance(reasoning, dict):
        if reasoning.get("enabled"):
            out["enable_thinking"] = True
    elif isinstance(reasoning, bool):
        out["enable_thinking"] = reasoning
    return out


def post_chat(
    body: dict[str, Any],
    *,
    timeout: int = 900,
    client_controls: bool | None = None,
) -> requests.Response:
    del client_controls
    prepared = prepare_chat_body(body)
    return requests.post(
        chat_completions_url(),
        json=prepared,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
