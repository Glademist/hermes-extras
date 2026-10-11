"""Laya-MLX first-stage backend for Prompt-Guard — WATCH MODE ONLY.

Calls the local laya_server (~/AI/example-spike/laya_server.py, port 8097).
Never modifies verdicts: results are logged to JSONL and attached to the
check() result as the "laya" field. Enforcement is a future, explicit opt-in.

Environment:
    PROMPT_GUARD_LAYA=off|watch  (default: watch when server is reachable)

Fail-open: any error → returns None, guard proceeds as before.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("prompt-guard-laya")

LAYA_URL = os.environ.get("PROMPT_GUARD_LAYA_URL", "http://127.0.0.1:8097/classify")
LAYA_TIMEOUT = float(os.environ.get("PROMPT_GUARD_LAYA_TIMEOUT", "3.0"))
LOG_PATH = Path.home() / "hermes-noemem" / "logs" / "laya-guard.jsonl"


def _env_mode() -> str:
    return os.environ.get("PROMPT_GUARD_LAYA", "watch").lower()


def health(timeout: float = 1.0) -> bool:
    """Cheap check that the laya server is up (cached for 60s)."""
    global _health_ok, _health_ts
    now = time.time()
    if now - _health_ts < 60:
        return _health_ok
    try:
        with urllib.request.urlopen(LAYA_URL.replace("/classify", "/health"), timeout=timeout):
            _health_ok, _health_ts = True, now
    except Exception:
        _health_ok, _health_ts = False, now
    return _health_ok


_health_ok, _health_ts = False, 0.0


def classify(text: str, source: str = "cli") -> Optional[Dict]:
    """Ask the local Laya server. Returns dict or None (fail-open).

    `source` labels where the scanned text came from ("cli", "web_search",
    "gmail-watch", ...) — recorded in the JSONL so a line can be traced back.
    """
    if _env_mode() == "off" or not health():
        return None
    try:
        t0 = time.perf_counter()
        req = urllib.request.Request(
            LAYA_URL,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=LAYA_TIMEOUT) as resp:
            data = json.loads(resp.read())
        if not data.get("ok"):
            return None
        dt = (time.perf_counter() - t0) * 1000
        result = {
            "choice": data["choice"],
            "attack": data["attack"],
            "model_ms": data.get("ms"),
            "rtt_ms": round(dt, 1),
        }
        _log(text, source, result)
        return result
    except Exception as e:
        logger.debug("laya backend unavailable: %s", e)
        _health_ok, _health_ts = False, time.time()  # back off until next 60s window
        return None


def _log(text: str, tool_name: str, result: Dict) -> None:
    """Append one observation line to the JSONL watch log (best effort)."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": tool_name,
            "head": text[:200].replace("\n", " "),
            **result,
        }
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
