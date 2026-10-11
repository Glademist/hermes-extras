#!/usr/bin/env python3
"""Thin client for the local MTPLX server.

Scripts should depend on the port (or MTPLX_BASE_URL), not a hardcoded model id.
The LaunchAgent plist owns which weights are loaded; requests omit ``model`` and
the server uses its active checkpoint.

Hermes-noemem archival scripts set ``enable_thinking`` per request and send
``X-MTPLX-Allow-Client-Controls`` so thinking works while the LaunchAgent
stays ``--reasoning off`` for other callers.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Optional

import requests

DEFAULT_BASE_URL = "http://localhost:8085"
CLIENT_CONTROLS_HEADER = "X-MTPLX-Allow-Client-Controls"


def base_url() -> str:
    return os.environ.get("MTPLX_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def chat_completions_url() -> str:
    return f"{base_url()}/v1/chat/completions"


def health_url() -> str:
    return f"{base_url()}/health"


@lru_cache(maxsize=1)
def active_model_id() -> Optional[str]:
    """Best-effort model id for logging; not required for API calls."""
    try:
        resp = requests.get(f"{base_url()}/v1/models", timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data") or []
        if data:
            return data[0].get("id")
    except Exception:
        pass
    try:
        resp = requests.get(health_url(), timeout=10)
        resp.raise_for_status()
        return resp.json().get("model")
    except Exception:
        return None


def parse_assistant_content(message: dict[str, Any]) -> str:
    """Return visible assistant text (never chain-of-thought)."""
    content = str(message.get("content") or "").strip()
    if content:
        return content
    reasoning = str(message.get("reasoning_content") or "").strip()
    if "</think>" in reasoning:
        return reasoning.split("</think>", 1)[-1].strip()
    return ""


def prepare_chat_body(body: dict[str, Any]) -> dict[str, Any]:
    """Drop hardcoded model; map legacy flags to ``enable_thinking``."""
    out = dict(body)
    out.pop("model", None)

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
    prepared = prepare_chat_body(body)
    if client_controls is None:
        client_controls = "enable_thinking" in prepared
    headers = {CLIENT_CONTROLS_HEADER: "true"} if client_controls else None
    return requests.post(
        chat_completions_url(),
        json=prepared,
        headers=headers,
        timeout=timeout,
    )