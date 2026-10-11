"""tool-output-cap — cap oversized tool results before they enter context.

Why: on a long session, tool results dominate the prompt (measured ~80% of a
100K-token context). RTK already shrinks `terminal` output; this plugin extends
the same economy to the other big text producers (read_file, web_extract,
execute_code, browser_exec, MCP getters).

Safety contract (a cap must never hide success/failure):
  * An ERROR result is never touched — errors stay fully visible.
  * We only truncate the MIDDLE, keeping the head AND the tail, so the status
    line (top) and the key result/footer (bottom) survive.
  * Every truncation appends an explicit, unmissable marker.
  * Small results (< threshold) are untouched.
  * JSON dicts: only the single dominant text field is truncated and the dict is
    re-serialised, so structural fields (exit_code, success, ...) are preserved
    and the JSON stays valid. Anything else is left alone (fail open).
  * The plugin fails open: any exception -> return None (pass through).

Modes (config.json -> "mode"):
  * "dry_run" (default): decide + log, but never modify. Safe to enable first.
  * "on": apply the cap.

Runs on `transform_tool_result`, which `model_tools.py::_apply_transform_tool_result_hook`
fires AFTER the tool and BEFORE the result is persisted — so capping here does
NOT invalidate the prompt cache.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_PLUGIN_DIR, "config.json")

# JSON string fields that commonly hold the bulk of a result's text.
_TEXT_FIELDS = ("output", "content", "result", "text", "stdout", "data", "markdown")

_DEFAULTS: Dict[str, Any] = {
    "mode": "dry_run",
    "threshold_chars": 24_000,
    "head_chars": 12_000,
    "tail_chars": 6_000,
    "log_path": "~/.hermes/logs/tool-output-cap.jsonl",
    "target_tools": [],
}

# Loaded once; tests pass an explicit policy to cap_result().
_POLICY_CACHE: Optional[Dict[str, Any]] = None


def _load_policy(force: bool = False) -> Dict[str, Any]:
    """Read config.json merged over defaults. Fail-open to defaults."""
    global _POLICY_CACHE
    if _POLICY_CACHE is not None and not force:
        return _POLICY_CACHE
    policy = dict(_DEFAULTS)
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            policy.update(loaded)
    except Exception:
        pass  # missing/unreadable config -> defaults (dry_run, no tools targeted)
    _POLICY_CACHE = policy
    return policy


def _marker(head: int, tail: int, total: int) -> str:
    omitted = max(0, total - head - tail)
    return (
        f"\n\n…[tool-output-cap] TRUNCATED: showing first {head} + last {tail} of "
        f"{total} chars ({omitted} omitted to save context).\n"
        "The result is NOT complete. To see the omitted middle, re-run the tool "
        "with a narrower scope (e.g. read_file offset/limit, web_extract "
        "char_limit, a smaller search/range).\n\n"
    )


def _cap_text(text: str, head: int, tail: int) -> str:
    """Head + marker + tail. Assumes len(text) > head + tail."""
    return text[:head] + _marker(head, tail, len(text)) + text[-tail:]


def _dominant_text_field(obj: Dict[str, Any], threshold: int) -> Optional[str]:
    """The one string field that makes a JSON dict oversized, or None."""
    best_name, best_len = None, 0
    for name in _TEXT_FIELDS:
        value = obj.get(name)
        if isinstance(value, str) and len(value) > best_len:
            best_name, best_len = name, len(value)
    # Only cap when that field alone is over threshold (i.e. it IS the bulk).
    return best_name if best_name and best_len > threshold else None


def cap_result(
    tool_name: str, result: Any, status: Optional[str], policy: Dict[str, Any]
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Pure decision. Returns (new_result_or_None, meta).

    ``new_result`` is None when nothing should change (small, error, untargeted,
    or an unsupported shape). ``meta`` always describes the decision, for logging.
    """
    meta: Dict[str, Any] = {"tool": tool_name, "action": "skip", "reason": "", "status": status}
    if not isinstance(result, str) or not result:
        meta["reason"] = "non-string"
        return None, meta

    total = len(result)
    meta["orig_chars"] = total

    targets = policy.get("target_tools") or []
    if targets and tool_name not in targets:
        meta["reason"] = "untargeted"
        return None, meta

    if status and status != "ok":
        meta["reason"] = "error-result-preserved"
        return None, meta

    threshold = int(policy.get("threshold_chars") or _DEFAULTS["threshold_chars"])
    if total <= threshold:
        meta["reason"] = "under-threshold"
        return None, meta

    head = int(policy.get("head_chars") or _DEFAULTS["head_chars"])
    tail = int(policy.get("tail_chars") or _DEFAULTS["tail_chars"])
    if head + tail >= total:
        meta["reason"] = "head+tail-covers-all"
        return None, meta

    # JSON dict: truncate only the dominant text field, keep structure valid.
    try:
        parsed = json.loads(result)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        field = _dominant_text_field(parsed, threshold)
        if field is None:
            meta["reason"] = "json-no-dominant-text-field"
            return None, meta
        value = parsed[field]
        parsed[field] = _cap_text(value, head, tail)
        new = json.dumps(parsed, ensure_ascii=False)
        meta.update(action="cap", reason="json-text-field", field=field, new_chars=len(new))
        return new, meta

    if parsed is not None:
        # A JSON list/scalar (e.g. search_files) — leave it alone (conservative).
        meta["reason"] = "json-non-dict"
        return None, meta

    # Plain text/markdown.
    new = _cap_text(result, head, tail)
    meta.update(action="cap", reason="plain-text", new_chars=len(new))
    return new, meta


def _log(meta: Dict[str, Any], mode: str) -> None:
    """Append one JSONL record. Never raises."""
    try:
        policy = _load_policy()
        path = os.path.expanduser(str(policy.get("log_path") or _DEFAULTS["log_path"]))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": mode, **meta}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _on_transform_tool_result(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    status: Optional[str] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    **_: Any,
) -> Optional[str]:
    """transform_tool_result handler: return capped text, or None to pass through."""
    try:
        policy = _load_policy()
        new, meta = cap_result(tool_name, result, status, policy)
        if meta.get("action") == "cap":
            mode = str(policy.get("mode") or "dry_run")
            _log(meta, mode)
            if mode == "on":
                return new
        return None
    except Exception:
        return None  # fail open — never break the tool path


def register(ctx) -> None:
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)