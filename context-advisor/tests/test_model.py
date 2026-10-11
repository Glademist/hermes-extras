"""Unit tests for the pure core + renderer.

Run either way:
    ~/.hermes/hermes-agent/venv/bin/python tests/test_model.py     # standalone
    ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -q     # pytest

The numeric fixtures are REAL figures measured from the live session DB on
2026-10-07 (see tools/probe.py), so the tests double as a regression against the
exact discrepancy this project exists to fix.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advisor import Budgets, Snapshot, build_pressure, level_for
from advisor.render import fmt_tokens, render_footer

WINDOW_1M = 1_048_576  # deepseek-v4.1-flash @ nous


def _pressure(tokens, msgs, source="anchored", window=WINDOW_1M):
    return build_pressure(
        Snapshot(tokens=tokens, source=source, message_count=msgs, budgets=Budgets(window=window))
    )


def test_budgets_effective_is_compressor_threshold():
    b = Budgets(window=WINDOW_1M)
    assert b.compress_tokens == 524288          # 1M * 0.50 — the real budget
    assert b.hygiene_tokens == 891289           # 1M * 0.85 — the safety net
    assert b.effective_tokens == b.compress_tokens
    assert b.hygiene_tokens > b.compress_tokens


def test_budgets_rejects_incoherent_knobs():
    for bad in (dict(window=0), dict(window=100, compress_ratio=1.5),
                dict(window=100, hygiene_ratio=0.4), dict(window=100, msg_limit=0)):
        try:
            Budgets(**{"window": WINDOW_1M, **bad})
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")


def test_live_session_is_quiet():
    # 'Design context advisory plugin' — 61 msgs, anchored ~105K.
    p = _pressure(104922, 61)
    assert p.level == "ok"
    assert p.driver == "tokens"
    assert 19 <= p.token_pct <= 21               # ~20% of the 524K budget
    assert "handoff" not in render_footer(p)


def test_web_session_is_near_the_real_budget():
    # 'Předělat a sjednotit web pro děti' — 214 msgs, anchored 479306.
    # The footer used to call this '/1M'; it is 91% of the 524K auto-compress budget.
    p = _pressure(479306, 214)
    assert p.level == "near"
    assert p.driver == "tokens"
    assert p.token_pct >= 90
    line = render_footer(p)
    assert "91%" in line and "handoff + /compress recommended" in line


def test_message_valve_can_be_the_binding_axis():
    # 'Vybrat další předmět' — 323 msgs: 81% of the 400 hard valve even at low tokens.
    p = _pressure(120_000, 340)
    assert p.driver == "messages"
    assert p.level == "near"                      # 340/400 = 85%
    assert "⚠" in render_footer(p)


def test_message_valve_fires_at_limit():
    p = _pressure(50_000, 400)
    assert p.level == "imminent"
    assert p.driver == "messages"


def test_level_boundaries():
    assert level_for(0.59) == "ok"
    assert level_for(0.60) == "approaching"
    assert level_for(0.849) == "approaching"
    assert level_for(0.85) == "near"
    assert level_for(0.999) == "near"
    assert level_for(1.0) == "imminent"


def test_fmt_tokens():
    assert fmt_tokens(524288) == "524K"
    assert fmt_tokens(1_048_576) == "1M"
    assert fmt_tokens(1_500_000) == "1.5M"
    assert fmt_tokens(999) == "999"


def test_provenance_is_surfaced():
    # A leading '~' marks a LOCAL ESTIMATE; the anchored/provider default carries no label.
    anchored = render_footer(_pressure(200_000, 10, source="anchored"))
    estimate = render_footer(_pressure(200_000, 10, source="estimate"))
    assert anchored.startswith("Ctx: 38%") and "~" not in anchored   # 200000/524288 = 38.1% -> 38
    assert estimate.startswith("Ctx: ~38%")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
