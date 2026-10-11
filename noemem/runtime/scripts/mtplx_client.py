#!/usr/bin/env python3
"""Thin client for LLM inference backend.

Routes based on ``LLM_BACKEND``:

  nous        (default) → nous_client.py
      deepseek/deepseek-v4-flash via Nous Portal; JWT mint/refresh on each resolve,
      automatic one-shot retry on HTTP 401.

  hermes                → hermes_client.py
      Current Hermes main model from ~/.hermes/config.yaml (e.g. grok-4.5 / xai-oauth).

  darkbloom             → darkbloom_client.py (local MLX self-route, gemma-4-26b)
  mtplx                 → mtplx_local.py (:8085)

All hermes-noemem LLM scripts import from this module — routing is transparent.
"""

from __future__ import annotations

import os
from typing import Any, Optional


def _resolve_backend():
    backend = os.environ.get("LLM_BACKEND", "nous").strip().lower()
    if backend in ("hermes", "main", "current"):
        import hermes_client  # type: ignore[import-untyped]

        return hermes_client
    if backend == "nous":
        import nous_client  # type: ignore[import-untyped]

        return nous_client
    if backend in ("darkbloom", "db", "eigen"):
        import darkbloom_client  # type: ignore[import-untyped]

        return darkbloom_client
    if backend in ("gemma_local", "gemma", "local_gemma", "gl"):
        import gemma_local_client  # type: ignore[import-untyped]

        return gemma_local_client
    if backend in ("nex_local", "nex", "local_nex", "nx"):
        import nex_local_client  # type: ignore[import-untyped]

        return nex_local_client
    import mtplx_local  # type: ignore[import-untyped]

    return mtplx_local


_backend = _resolve_backend()


def base_url() -> str:
    return _backend.base_url()


def chat_completions_url() -> str:
    return _backend.chat_completions_url()


def active_model_id() -> Optional[str]:
    return _backend.active_model_id()


def parse_assistant_content(message: dict[str, Any]) -> str:
    return _backend.parse_assistant_content(message)


def prepare_chat_body(body: dict[str, Any]) -> dict[str, Any]:
    return _backend.prepare_chat_body(body)


def post_chat(
    body: dict[str, Any],
    *,
    timeout: int = 900,
    client_controls: bool | None = None,
) -> Any:
    return _backend.post_chat(body, timeout=timeout, client_controls=client_controls)
