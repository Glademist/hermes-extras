"""Best-effort usage-anchored figure for a plugin (no live agent in hand).

A plugin's ``post_api_request`` gives us ``session_id`` and ``usage`` but NOT the
agent object, so the anchored figure is read from the session DB exactly the way
gateway hygiene does it (``gateway/run_turn.py::_hmwa_hygiene_plan`` ->
``agent.usage_anchor.persisted_anchor_tokens``).

Fingerprint caveat: ``persisted_anchor_tokens`` honours the persisted anchor only
while the DB-loaded transcript still matches the anchor's fingerprint (to catch
compaction/splice/rewind). In the live probe that held for 5 of 6 sessions and
returned ``None`` for the sixth — so every path here fails OPEN to the raw
provider figure, which is a safe UPPER bound on pressure.
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple

# (session_id, turn_id) -> (tokens, source). The anchored figure is turn-stable,
# but post_api_request fires once per API call per turn; compute it once.
_LOCK = threading.Lock()
_ANCHOR_CACHE: Dict[Tuple[str, str], Tuple[int, str]] = {}
_MAX = 256


def _evict_one() -> None:
    try:
        _ANCHOR_CACHE.pop(next(iter(_ANCHOR_CACHE)), None)
    except (StopIteration, RuntimeError):
        pass


def _read_anchor(session_id: str) -> Optional[int]:
    """Anchored tokens from the session row, or None when absent/unreadable/stale."""
    import hermes_state_registry
    from agent.usage_anchor import persisted_anchor_tokens

    db = hermes_state_registry.acquire()
    try:
        messages = db.get_messages_as_conversation(session_id)
        if not messages:
            return None
        return persisted_anchor_tokens(db, session_id, messages)
    finally:
        hermes_state_registry.release(db)


def resolve(
    session_id: str,
    turn_id: str = "",
    provider_tokens: Optional[int] = None,
) -> Tuple[Optional[int], str]:
    """Return ``(tokens, source)`` for this turn.

    Prefers the anchored figure (matches ``/context`` and what the compressor
    fires on); falls back to the raw provider count, then to nothing. ``source``
    is ``"anchored"``, ``"provider"``, or ``"none"``.
    """
    key = (session_id or "", turn_id or "")
    with _LOCK:
        cached = _ANCHOR_CACHE.get(key)
    if cached is not None:
        tokens, source = cached
        # The anchor is turn-stable; only the provider fallback can be absent early.
        if tokens > 0:
            return tokens, source

    anchored: Optional[int] = None
    if session_id:
        try:
            anchored = _read_anchor(session_id)
        except Exception:
            anchored = None

    result: Tuple[Optional[int], str]
    if isinstance(anchored, int) and anchored > 0:
        result = (anchored, "anchored")
    elif isinstance(provider_tokens, int) and provider_tokens > 0:
        result = (provider_tokens, "provider")
    else:
        result = (None, "none")

    with _LOCK:
        if len(_ANCHOR_CACHE) >= _MAX and key not in _ANCHOR_CACHE:
            _evict_one()
        _ANCHOR_CACHE[key] = result
    return result


def clear(session_id: str = "") -> None:
    """Drop cached entries (all, or one session's)."""
    with _LOCK:
        if not session_id:
            _ANCHOR_CACHE.clear()
            return
        for k in [k for k in _ANCHOR_CACHE if k[0] == session_id]:
            _ANCHOR_CACHE.pop(k, None)
