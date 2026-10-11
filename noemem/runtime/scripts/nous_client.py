#!/usr/bin/env python3
"""nous_client.py — OpenAI-compatible client for Nous Inference API.

LLM_BACKEND=nous. Credentials via Hermes credential pool + runtime resolve
(so agent_key / short-lived JWTs refresh correctly — raw access_token alone
caused 401s when the mint expired overnight).

Default model: deepseek/deepseek-v4-flash-0731 (pinned snapshot; override HERMES_NOEMEM_MODEL /
NOUS_MODEL). Pinned 2026-10-08 to match Hermes' own deepseek snapshot and stop the alias
changing underfoot — the reasoning behaviour is identical across the flash aliases, so the pin
is identity hygiene, not a fix.
"""

from __future__ import annotations

import json
import os
import subprocess
from functools import lru_cache
from typing import Any, Optional

import requests

_HERMES_VENV_PYTHON = "~/.hermes/hermes-agent/venv/bin/python3"
_HERMES_AGENT_ROOT = "~/.hermes/hermes-agent"

DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"
DEFAULT_BASE_URL = "https://inference-api.nousresearch.com/v1"


def _resolve_runtime(*, force: bool = False) -> dict:
    if force:
        _resolve_runtime_cached.cache_clear()
    return _resolve_runtime_cached()


@lru_cache(maxsize=1)
def _resolve_runtime_cached() -> dict:
    """Use Hermes resolve_runtime_provider('nous') so JWT is minted/refreshed."""
    code = r"""
import json, os, sys
sys.path.insert(0, os.environ["HERMES_AGENT_ROOT"])
from hermes_cli.runtime_provider import resolve_runtime_provider
rp = resolve_runtime_provider(requested="nous")
print(json.dumps({
    "base_url": (rp.get("base_url") or "").rstrip("/") or "https://inference-api.nousresearch.com/v1",
    "api_key": rp.get("api_key") or "",
    "provider": rp.get("provider"),
    "source": rp.get("source"),
}))
"""
    env = os.environ.copy()
    env["HERMES_AGENT_ROOT"] = _HERMES_AGENT_ROOT
    result = subprocess.run(
        [_HERMES_VENV_PYTHON, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to resolve Nous credentials via Hermes: "
            + (result.stderr or result.stdout or "").strip()[:500]
        )
    data = json.loads((result.stdout or "").strip().splitlines()[-1])
    if not data.get("api_key"):
        raise RuntimeError("Nous runtime returned empty api_key")
    return data


def base_url() -> str:
    return _resolve_runtime().get("base_url") or DEFAULT_BASE_URL


def chat_completions_url() -> str:
    return f"{base_url()}/chat/completions"


@lru_cache(maxsize=1)
def active_model_id() -> str:
    return (
        os.environ.get("HERMES_NOEMEM_MODEL")
        or os.environ.get("NOUS_MODEL")
        or DEFAULT_MODEL
    ).strip() or DEFAULT_MODEL


def parse_assistant_content(message: dict[str, Any]) -> str:
    content = str(message.get("content") or "").strip()
    if content:
        return content
    reasoning = str(message.get("reasoning") or "").strip()
    if " response" in reasoning:
        return reasoning.split(" response", 1)[-1].strip()
    return ""


def prepare_chat_body(body: dict[str, Any]) -> dict[str, Any]:
    out = dict(body)
    if "model" not in out:
        out["model"] = active_model_id()
    # The Nous deepseek endpoints do NOT accept `enable_thinking` — it is absent from
    # supported_parameters and is silently IGNORED, so a request that "turned thinking
    # off" that way actually ran with thinking ON at default_effort=high and burned the
    # whole max_tokens on reasoning (finish_reason=length, empty content). Measured
    # 2026-10-08: enable_thinking=false left reasoning_tokens == max_tokens on BOTH
    # deepseek-v4-flash and -flash-0731, while `reasoning_effort="none"` (equivalently
    # `reasoning={"enabled": false}`) gave reasoning=0 with a full answer. Translate the
    # caller's intent into the switch the endpoint honours.
    thinking = out.pop("enable_thinking", None)
    reasoning = out.pop("reasoning", None)
    if thinking is None and isinstance(reasoning, dict):
        thinking = reasoning.get("enabled")
    elif thinking is None and isinstance(reasoning, bool):
        thinking = reasoning
    if thinking is False:
        out["reasoning_effort"] = "none"
    return out


def post_chat(
    body: dict[str, Any],
    *,
    timeout: int = 900,
    client_controls: bool | None = None,
) -> requests.Response:
    del client_controls
    prepared = prepare_chat_body(body)

    def _do(runtime: dict) -> requests.Response:
        headers = {
            "Authorization": f"Bearer {runtime['api_key']}",
            "Content-Type": "application/json",
        }
        return requests.post(
            f"{runtime['base_url'].rstrip('/')}/chat/completions",
            json=prepared,
            headers=headers,
            timeout=timeout,
        )

    runtime = _resolve_runtime(force=False)
    resp = _do(runtime)
    if resp.status_code == 401:
        active_model_id.cache_clear()
        runtime = _resolve_runtime(force=True)
        resp = _do(runtime)
    _capture_reasoning(resp)
    return resp


def _capture_reasoning(resp: requests.Response) -> None:
    """Debug hook: dump raw model reasoning to a file when NOUS_REASONING_LOG set.

    Production behavior is unchanged unless the env var is present. Used to
    audit WHY a model assigned (or left blank) a PROJEKT — lets us read the
    chain-of-thought that led to a classification, not just the final answer.
    """
    path = os.environ.get("NOUS_REASONING_LOG")
    if not path or resp.status_code != 200:
        return
    try:
        msg = resp.json()["choices"][0]["message"]
    except Exception:
        return
    try:
        reasoning = str(msg.get("reasoning") or "")
    except Exception:
        reasoning = ""
    if not reasoning:
        return
    model = active_model_id()
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"### model={model}\n{reasoning}\n\n---\n")
    except OSError:
        pass
