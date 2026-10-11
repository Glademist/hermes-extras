#!/usr/bin/env python3
"""hermes_client.py — use the *current* Hermes main model for noemem scripts.

LLM_BACKEND=hermes resolves credentials the same way Hermes chat does:
  ~/.hermes/config.yaml → model.provider + model.default
  hermes_cli.runtime_provider.resolve_runtime_provider()
  OAuth pools refresh short-lived JWTs (Nous / xAI OAuth)

Subprocess into Hermes venv (Python 3.11) so credential_pool type syntax works
from system Python 3.9 callers.

Overrides:
  HERMES_NOEMEM_PROVIDER  force provider (e.g. nous, xai-oauth)
  HERMES_NOEMEM_MODEL     force model id (e.g. grok-4.5, deepseek/deepseek-v4-flash)
  HERMES_HOME             default ~/.hermes
"""

from __future__ import annotations

import json
import os
import subprocess
from functools import lru_cache
from typing import Any, Optional

import requests
import yaml
from pathlib import Path

_HERMES_VENV_PYTHON = "~/.hermes/hermes-agent/venv/bin/python3"
_HERMES_AGENT_ROOT = "~/.hermes/hermes-agent"


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def _read_model_config() -> dict:
    cfg_path = _hermes_home() / "config.yaml"
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except OSError:
        data = {}
    model = data.get("model") or {}
    if not isinstance(model, dict):
        model = {}
    return model


def _resolve_via_hermes_venv(provider: Optional[str]) -> dict:
    """Mint/refresh credentials through Hermes runtime (handles OAuth TTL)."""
    code = r"""
import json, os, sys
sys.path.insert(0, os.environ["HERMES_AGENT_ROOT"])
from hermes_cli.runtime_provider import resolve_runtime_provider
requested = os.environ.get("HERMES_REQ_PROVIDER") or None
if requested == "":
    requested = None
rp = resolve_runtime_provider(requested=requested)
out = {
    "provider": rp.get("provider"),
    "base_url": (rp.get("base_url") or "").rstrip("/"),
    "api_key": rp.get("api_key") or "",
    "api_mode": rp.get("api_mode") or "chat_completions",
    "source": rp.get("source"),
}
print(json.dumps(out))
"""
    env = os.environ.copy()
    env["HERMES_AGENT_ROOT"] = _HERMES_AGENT_ROOT
    if provider:
        env["HERMES_REQ_PROVIDER"] = provider
    else:
        env.pop("HERMES_REQ_PROVIDER", None)
    result = subprocess.run(
        [_HERMES_VENV_PYTHON, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Hermes runtime credential resolve failed: "
            + (result.stderr or result.stdout or "").strip()[:500]
        )
    line = (result.stdout or "").strip().splitlines()[-1]
    data = json.loads(line)
    if not data.get("api_key") or not data.get("base_url"):
        raise RuntimeError(f"Hermes runtime returned incomplete credentials: {data!r}")
    return data


def _resolve_runtime(*, force: bool = False) -> dict:
    """Cached resolve; force=True after 401."""
    if force:
        _resolve_runtime_cached.cache_clear()
    return _resolve_runtime_cached()


@lru_cache(maxsize=1)
def _resolve_runtime_cached() -> dict:
    model_cfg = _read_model_config()
    provider = (
        os.environ.get("HERMES_NOEMEM_PROVIDER")
        or model_cfg.get("provider")
        or None
    )
    model = (
        os.environ.get("HERMES_NOEMEM_MODEL")
        or model_cfg.get("default")
        or model_cfg.get("model")
        or ""
    )
    runtime = _resolve_via_hermes_venv(provider)
    runtime["model"] = str(model).strip()
    return runtime


def base_url() -> str:
    return _resolve_runtime()["base_url"]


def chat_completions_url() -> str:
    return f"{base_url()}/chat/completions"


def health_url() -> str:
    return f"{base_url()}/models"


@lru_cache(maxsize=1)
def active_model_id() -> str:
    model = _resolve_runtime().get("model") or ""
    if model:
        return model
    # last resort
    return "unknown"


def parse_assistant_content(message: dict[str, Any]) -> str:
    content = str(message.get("content") or "").strip()
    if content:
        return content
    # tolerate reasoning-style fields
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
    out = dict(body)
    if not out.get("model"):
        mid = active_model_id()
        if mid and mid != "unknown":
            out["model"] = mid

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
    """POST /chat/completions with Hermes-resolved credentials; 401 → refresh once."""
    del client_controls
    prepared = prepare_chat_body(body)

    def _do(runtime: dict) -> requests.Response:
        headers = {
            "Authorization": f"Bearer {runtime['api_key']}",
            "Content-Type": "application/json",
        }
        url = f"{runtime['base_url'].rstrip('/')}/chat/completions"
        # ensure model present after refresh
        payload = dict(prepared)
        if not payload.get("model") and runtime.get("model"):
            payload["model"] = runtime["model"]
        return requests.post(url, json=payload, headers=headers, timeout=timeout)

    runtime = _resolve_runtime(force=False)
    resp = _do(runtime)
    if resp.status_code == 401:
        # OAuth JWT expired — mint fresh via Hermes runtime and retry once
        active_model_id.cache_clear()
        runtime = _resolve_runtime(force=True)
        resp = _do(runtime)
    return resp
