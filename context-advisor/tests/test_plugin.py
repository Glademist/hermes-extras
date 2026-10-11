"""Smoke-test the PLUGIN handlers themselves (no gateway, no restart).

Loads ~/.hermes/plugins/context-advisor by path and calls its hook handlers with
kwargs shaped like the real ones. Proves the wiring the unit core does not:
replace-vs-delta, platform gating, idempotency, fail-open on missing data.

The plugin uses the hook's own provider figure (no session-DB read) — see the
loader-probe finding in design.md §5.1.

Run: ~/.hermes/hermes-agent/venv/bin/python tests/test_plugin.py
"""

import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PLUGIN = os.path.expanduser("~/.hermes/plugins/context-advisor/__init__.py")
WIN = 1_048_576


def _load():
    spec = importlib.util.spec_from_file_location("context_advisor_plugin", _PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Deterministic window without touching the model cache or a live gateway.
    mod.__dict__["_window_for"] = lambda model, base: WIN
    mod.__dict__["_is_live_voice_chat"] = lambda sid: False
    return mod


def test_footer_appended_and_replaces_text():
    m = _load()
    # 480000 / 524288 = 91.6% -> 92;  214/400 = 53.5% -> 54
    m._on_post_api_request(session_id="s1", turn_id="t1", model="m", base_url="b",
                           message_count=214, usage={"prompt_tokens": 480000})
    out = m._on_transform_llm_output(response_text="Hello there.", session_id="s1", platform="discord")
    assert out is not None and out.startswith("Hello there."), "must keep the original text"
    assert "Ctx: 92%" in out and "Msg: 54%" in out and "handoff + /compress recommended" in out
    return out


def test_idempotent():
    m = _load()
    m._on_post_api_request(session_id="s2", turn_id="t2", message_count=10,
                           usage={"prompt_tokens": 200000})
    first = m._on_transform_llm_output(response_text="Hi.", session_id="s2", platform="discord")
    assert first is not None
    second = m._on_transform_llm_output(response_text=first, session_id="s2", platform="discord")
    assert second is None, "re-entrant call must not double the footer"


def test_platform_gating():
    m = _load()
    m._on_post_api_request(session_id="s3", turn_id="t3", message_count=10,
                           usage={"prompt_tokens": 200000})
    assert m._on_transform_llm_output(response_text="Hi.", session_id="s3", platform="telegram") is None
    assert m._on_transform_llm_output(response_text="Hi.", session_id="s3", platform="cli") is None


def test_voice_suppressed():
    m = _load()
    m.__dict__["_is_live_voice_chat"] = lambda sid: True
    m._on_post_api_request(session_id="s4", turn_id="t4", message_count=10,
                           usage={"prompt_tokens": 200000})
    assert m._on_transform_llm_output(response_text="Hi.", session_id="s4", platform="discord") is None


def test_fail_open_without_usage():
    m = _load()
    # No usage recorded for this session -> must no-op, never raise.
    assert m._on_transform_llm_output(response_text="Hi.", session_id="unknown", platform="discord") is None


def test_bad_usage_is_ignored():
    m = _load()
    for bad in (None, {}, {"prompt_tokens": "x"}, {"prompt_tokens": -1}, {"prompt_tokens": 0}):
        m._on_post_api_request(session_id="s5", turn_id="t5", usage=bad)
    assert "s5" not in m._LEDGER or m._LEDGER["s5"]["provider"] == 0


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sample = None
    try:
        sample = test_footer_appended_and_replaces_text()
    except Exception as exc:  # noqa: BLE001
        print("sample render failed:", exc)
    print("\n--- sample footer line ---")
    print(sample)
    print()
    sys.exit(1 if _run_all() else 0)
