"""Pure context-pressure model.

The whole point: Hermes has THREE measurement layers, each comparing a different
figure against a different denominator. The footer used to show the raw window
(``/1M``) and none of the effective ceilings, so a session at 91% of the real
auto-compress budget looked idle.

    layer            figure (best available)              denominator
    ---------------- ------------------------------------ -----------------------
    agent compressor usage-anchored (provider + delta)   window * 0.50
    gateway hygiene  last_prompt_tokens -> anchored      window * 0.85
    message valve    len(history)                        hygiene_hard_message_limit

Effective token budget = the EARLIEST token event = compressor threshold.
Reference: agent/context_breakdown.py, agent/usage_anchor.py,
gateway/run_turn.py::_hmwa_hygiene_plan, agent/context_compressor.py::threshold_tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Budgets:
    """The effective ceilings for one (model, config) pair.

    ``window`` is the raw model context window. The ratios mirror Hermes' own
    defaults; both are read from config.yaml by the caller so this stays pure.
    """

    window: int
    compress_ratio: float = 0.50   # compression.threshold
    hygiene_ratio: float = 0.85    # gateway hygiene (fixed 0.85, not user-tunable)
    msg_limit: int = 400           # compression.hygiene_hard_message_limit

    @property
    def compress_tokens(self) -> int:
        """Auto-compress fires here — the earliest token event, i.e. the real budget."""
        return int(self.window * self.compress_ratio)

    @property
    def hygiene_tokens(self) -> int:
        """Between-turn safety net (higher than the compressor; last resort)."""
        return int(self.window * self.hygiene_ratio)

    @property
    def effective_tokens(self) -> int:
        return self.compress_tokens

    def __post_init__(self) -> None:  # validate loudly rather than silently mis-scale
        if self.window <= 0:
            raise ValueError("window must be positive")
        if not (0.0 < self.compress_ratio < 1.0):
            raise ValueError("compress_ratio out of range")
        if not (0.0 < self.hygiene_ratio <= 1.0):
            raise ValueError("hygiene_ratio out of range")
        if self.hygiene_ratio <= self.compress_ratio:
            raise ValueError("hygiene threshold must exceed the compressor threshold")
        if self.msg_limit <= 0:
            raise ValueError("msg_limit must be positive")


@dataclass(frozen=True)
class Snapshot:
    """One turn's raw measurements, before any budget math.

    ``tokens`` is the chosen figure; ``source`` names its provenance so the
    rendered line never over-claims precision:

      anchored      provider usage + estimate of what was appended since
                    (matches /context and what the compressor triggers on)
      provider      raw usage.prompt_tokens of the last call (upper bound on
                    reasoning models: replayed thinking inflates it)
      estimate      local chars/4 guess (usage-less provider / post-restart)
    """

    tokens: int
    source: str
    message_count: int
    budgets: Budgets


@dataclass(frozen=True)
class Pressure:
    tokens: int
    source: str
    budgets: Budgets
    message_count: int
    token_ratio: float
    msg_ratio: float
    driver: str          # "tokens" | "messages" — whichever is closer to its ceiling
    ratio: float         # max(token_ratio, msg_ratio)
    level: str           # "ok" | "approaching" | "near" | "imminent"

    @property
    def token_pct(self) -> int:
        return round(self.token_ratio * 100)

    @property
    def msg_pct(self) -> int:
        return round(self.msg_ratio * 100)


# Level cut points. Chosen so the footer stays silent until there is a decision
# to make: "approaching" is informational, "near" carries the recommendation.
LEVELS = ("ok", "approaching", "near", "imminent")
_APPROACHING = 0.60
_NEAR = 0.85
_IMMINENT = 1.00


def level_for(ratio: float) -> str:
    if ratio >= _IMMINENT:
        return "imminent"
    if ratio >= _NEAR:
        return "near"
    if ratio >= _APPROACHING:
        return "approaching"
    return "ok"


def build_pressure(snap: Snapshot) -> Pressure:
    """Combine the token axis and the message axis into one pressure reading.

    Both axes can fire the same event (the message valve is a hard safety valve
    that bypasses the token check), so the driver is whichever ratio is higher.
    """
    b = snap.budgets
    token_ratio = snap.tokens / b.effective_tokens if b.effective_tokens else 0.0
    msg_ratio = snap.message_count / b.msg_limit if b.msg_limit else 0.0
    driver = "messages" if msg_ratio > token_ratio else "tokens"
    ratio = max(token_ratio, msg_ratio)
    return Pressure(
        tokens=snap.tokens,
        source=snap.source,
        budgets=b,
        message_count=snap.message_count,
        token_ratio=token_ratio,
        msg_ratio=msg_ratio,
        driver=driver,
        ratio=ratio,
        level=level_for(ratio),
    )
