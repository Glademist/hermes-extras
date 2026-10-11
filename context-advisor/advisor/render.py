"""Footer / advisory rendering, plus config-backed budget resolution.

Kept separate from ``core.py`` so the pure math has no filesystem or YAML
dependency.
"""

from __future__ import annotations

import os
from typing import Optional

from .core import Budgets, Pressure

# Shown only once the reading is close enough that a decision is useful.
_RECOMMENDATION = "handoff + /compress recommended"

_DEFAULT_COMPRESS_RATIO = 0.50
_DEFAULT_MSG_LIMIT = 400
_HYGIENE_RATIO = 0.85  # fixed in gateway/run_turn.py; not a user knob


def fmt_tokens(n: int) -> str:
    """Compact token count: 524288 -> '524K', 1048576 -> '1M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{round(n / 1_000)}K"
    return str(n)


def _config_path() -> str:
    try:
        from hermes_constants import get_hermes_home  # Hermes-venv only
        return os.path.join(get_hermes_home(), "config.yaml")
    except Exception:
        return os.path.expanduser("~/.hermes/config.yaml")


def read_budgets(window: int) -> Budgets:
    """Budgets for ``window``, with the live compression knobs from config.yaml.

    Best-effort: any read/parse failure keeps Hermes' own defaults, so the footer
    never breaks a turn over a config quirk.
    """
    ratio, msg_limit = _DEFAULT_COMPRESS_RATIO, _DEFAULT_MSG_LIMIT
    try:
        import yaml  # present in the Hermes venv

        with open(_config_path(), "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        comp = cfg.get("compression") or {}
        raw_ratio = comp.get("threshold")
        if isinstance(raw_ratio, (int, float)) and 0.0 < float(raw_ratio) < 1.0:
            ratio = float(raw_ratio)
        raw_limit = comp.get("hygiene_hard_message_limit")
        if isinstance(raw_limit, int) and raw_limit > 0:
            msg_limit = raw_limit
    except Exception:
        pass
    return Budgets(window=window, compress_ratio=ratio, hygiene_ratio=_HYGIENE_RATIO, msg_limit=msg_limit)


def _mark(level: str) -> str:
    """Glyph only; the renderer adds the separators. Empty for a quiet reading."""
    return {"ok": "", "approaching": "⏳", "near": "⚠", "imminent": "🔴"}.get(level, "")


def render_footer(p: Pressure) -> str:
    """The single advisory line appended to a reply.

    Both axes as full-percent figures against their real ceiling::

        Ctx: 59% · Msg: 61% · ⚠ handoff + /compress recommended

    ``Ctx`` is token pressure against the *effective* budget (``window *
    compress_ratio``), not the raw window. ``Msg`` is the message-hard-limit axis.
    A leading ``~`` on the ``Ctx`` figure marks a LOCAL ESTIMATE; the anchored /
    provider default carries no label. Values are the true percentages (integer),
    never rounded to a bucket — the same numbers the verification log prints.
    """
    lead = "~" if p.source == "estimate" else ""
    line = f"Ctx: {lead}{p.token_pct}% · Msg: {p.msg_pct}%"
    mark = _mark(p.level)
    if mark:
        line += f" · {mark}"
    if p.level in ("near", "imminent"):
        line += f" {_RECOMMENDATION}"
    return line


def should_render(p: Optional[Pressure]) -> bool:
    """Suppress the footer when there is nothing worth saying (and no numbers)."""
    return p is not None and p.tokens > 0
