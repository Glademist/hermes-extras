"""context-advisor (Hermes plugin shim) — thin layer over ~/AI/context-advisor.

All the logic lives OUTSIDE ~/.hermes in ``~/AI/context-advisor/advisor/`` (the
house pattern: code in its own dir, Hermes gets only a thin plugin/skill layer).
This file only wires hooks to that package.

Two hooks, one per-session ledger:

``post_api_request``      the provider's real ``usage.prompt_tokens`` for the turn's
                          last call, plus ``message_count`` and the model window.
``transform_llm_output``  appends the truthful pressure line.

Safety: ``transform_llm_output`` runs in ``turn_finalizer.py`` AFTER
``_persist_step()``, so nothing appended here enters session history and prompt
caching is preserved. Discord is not a ``draft_stream_is_message`` adapter, so
there is no sealed native stream to reopen.

NOT enabled in config.yaml on install — a plugin also needs its name in
``plugins.enabled`` and a gateway restart. That is an explicit approval step.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes.plugins.context-advisor")

# --- bridge to the out-of-tree project -------------------------------------
# Where the `advisor` package lives. Override with CONTEXT_ADVISOR_HOME; the
# default assumes this repo/clone is at ~/AI/context-advisor (adjust for yours).
_PROJECT = os.environ.get("CONTEXT_ADVISOR_HOME") or os.path.expanduser("~/AI/context-advisor")
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)

from advisor import build_pressure, Budgets, Snapshot  # noqa: E402
from advisor.render import read_budgets, render_footer  # noqa: E402

# session_id -> {"provider": int, "messages": int, "window": int, "turn": str}
_LEDGER: Dict[str, Dict[str, Any]] = {}
_MAX_SESSIONS = 512
_CHAT_OF_SESSION: Dict[str, str] = {}


def _window_for(model: str, base_url: str) -> int:
    if not model:
        return 0
    try:
        from agent.model_metadata import get_cached_context_length
        return int(get_cached_context_length(model, base_url or "") or 0)
    except Exception:
        return 0


def _budgets_for_window(window: int) -> Budgets:
    try:
        return read_budgets(window)
    except Exception:
        return Budgets(window=window)


def _on_post_api_request(
    session_id: str = "",
    turn_id: str = "",
    model: str = "",
    base_url: str = "",
    message_count: int = 0,
    usage: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> None:
    """Record the real prompt-token count + message count for this session."""
    if not session_id or not isinstance(usage, dict):
        return
    used = usage.get("prompt_tokens") or 0
    if not isinstance(used, int) or used <= 0:
        return

    if len(_LEDGER) >= _MAX_SESSIONS and session_id not in _LEDGER:
        _LEDGER.pop(next(iter(_LEDGER)), None)

    entry = _LEDGER.setdefault(
        session_id, {"provider": 0, "messages": 0, "window": 0, "turn": ""}
    )
    entry["provider"] = used
    entry["turn"] = turn_id or entry.get("turn", "")
    if isinstance(message_count, int) and message_count > 0:
        entry["messages"] = message_count
    if not entry.get("window"):
        entry["window"] = _window_for(model, base_url)


def _pressure_for(window: int, provider: int, messages: int):
    """Build the pressure reading from the hook's own figures.

    The figure is the raw provider ``prompt_tokens`` of the turn's last call. The
    usage-anchored alternative (``advisor/session_db.py``) is NOT used in the hot
    path: the loader probe (2026-10-07) showed the persisted anchor fails to match
    the reloaded transcript for exactly the *active* sessions a footer is for, while
    it matches idle ones that need no warning. So a DB read per turn buys nothing
    here, and with output rounded to the nearest 5 the residual reasoning-inflation
    of the raw figure is immaterial. ``session_db.resolve`` stays available for
    offline probes.
    """
    if not (isinstance(provider, int) and provider > 0):
        return None
    snap = Snapshot(
        tokens=provider,
        source="provider",
        message_count=max(0, messages),
        budgets=_budgets_for_window(window),
    )
    return build_pressure(snap)


def _chat_id_for_session(session_id: str) -> str:
    if not session_id:
        return ""
    cached = _CHAT_OF_SESSION.get(session_id)
    if cached is not None:
        return cached
    try:
        import hermes_state_registry
        db = hermes_state_registry.acquire()
        try:
            row = db.get_session(session_id) or {}
        finally:
            hermes_state_registry.release(db)
        chat_id = str(row.get("chat_id") or "")
    except Exception:
        return ""
    if chat_id:
        _CHAT_OF_SESSION[session_id] = chat_id
    return chat_id


def _is_live_voice_chat(session_id: str) -> bool:
    """True when this session's chat is bound to a live Discord voice connection."""
    chat_id = _chat_id_for_session(session_id)
    if not chat_id:
        return False
    try:
        from gateway.config import Platform
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
        if runner is None:
            return False
        maps = [getattr(runner, "adapters", None) or {}]
        maps += list((getattr(runner, "_profile_adapters", None) or {}).values())
        for mapping in maps:
            adapter = (mapping or {}).get(Platform.DISCORD)
            bound = getattr(adapter, "_voice_text_channels", None)
            if not isinstance(bound, dict):
                continue
            for guild_id, text_chat_id in bound.items():
                if str(text_chat_id) == chat_id and adapter.is_in_voice_channel(guild_id):
                    return True
    except Exception:
        return False
    return False


def _on_transform_llm_output(
    response_text: str = "",
    session_id: str = "",
    platform: str = "",
    **_: Any,
) -> Optional[str]:
    """Append the advisory line. Returning a string REPLACES the text."""
    if not response_text or not response_text.strip():
        return None
    if (platform or "").lower() != "discord":
        return None
    if _is_live_voice_chat(session_id):
        return None
    entry = _LEDGER.get(session_id)
    if not entry:
        return None
    window, provider, messages = entry["window"], entry["provider"], entry["messages"]
    if window <= 0 or provider <= 0:
        return None
    pressure = _pressure_for(window, provider, messages)
    if pressure is None:
        return None
    footer = render_footer(pressure)
    # Verification aid: one line per turn showing the exact inputs behind the footer,
    # so the rendered figure can be checked against real numbers after a restart.
    logger.info(
        "context-advisor %s: Ctx=%d%% Msg=%d%% (prompt_tokens=%d message_count=%d "
        "window=%d budget=%d)",
        (session_id or "")[-8:], pressure.token_pct, pressure.msg_pct, pressure.tokens,
        pressure.message_count, window, pressure.budgets.effective_tokens,
    )
    if not footer or footer in response_text:
        return None
    return response_text.rstrip() + "\n\n" + footer


def _on_session_end(session_id: str = "", **_: Any) -> None:
    _LEDGER.pop(session_id, None)
    _CHAT_OF_SESSION.pop(session_id, None)


def register(ctx) -> None:
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("transform_llm_output", _on_transform_llm_output)
    ctx.register_hook("on_session_end", _on_session_end)
