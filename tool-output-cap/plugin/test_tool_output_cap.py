"""Standalone tests for the tool-output-cap plugin.

Run:  python3 ~/.hermes/plugins/tool-output-cap/test_tool_output_cap.py
Exits non-zero on the first failure. No pytest needed.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import __init__ as plug  # noqa: E402  (the plugin module, loaded by path)

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        FAILS.append(name)


POLICY = {
    "mode": "on",
    "threshold_chars": 1000,
    "head_chars": 400,
    "tail_chars": 200,
    "target_tools": ["read_file", "web_extract", "execute_code", "terminal", "browser_exec", "search_files"],
    "log_path": "/tmp/tool-output-cap-test.jsonl",
}


def main():
    # 1. small result -> untouched
    _, m = plug.cap_result("read_file", "x" * 500, "ok", POLICY)
    check("small result untouched", m["action"] == "skip" and m["reason"] == "under-threshold", m)

    # 2. big ERROR result -> preserved
    big_err = "E" * 5000
    out, m = plug.cap_result("terminal", big_err, "error", POLICY)
    check("error result preserved", out is None and m["reason"] == "error-result-preserved", m)

    # 3. big plain text -> head+marker+tail, reduced, marker present
    body = "H" * 400 + "M" * 3000 + "T" * 200
    out, m = plug.cap_result("read_file", body, "ok", POLICY)
    check("plain text capped", isinstance(out, str) and m["action"] == "cap" and m["reason"] == "plain-text", m)
    check("plain text head kept", out.startswith("H" * 400), out[:20])
    check("plain text tail kept", out.endswith("T" * 200), out[-20:])
    check("plain text marker present", "TRUNCATED" in out and "tool-output-cap" in out, "")
    check("plain text smaller", len(out) < len(body), f"{len(out)} vs {len(body)}")

    # 4. big JSON dict with a dominant 'output' field -> valid JSON, field capped, exit_code kept
    payload = {"output": "O" * 5000, "exit_code": 0, "session_id": "abc"}
    out, m = plug.cap_result("terminal", json.dumps(payload), "ok", POLICY)
    check("json dict capped", isinstance(out, str) and m["reason"] == "json-text-field" and m["field"] == "output", m)
    reparsed = json.loads(out)  # must remain valid JSON
    check("json stays valid", isinstance(reparsed, dict), "")
    check("json exit_code preserved", reparsed.get("exit_code") == 0 and reparsed.get("session_id") == "abc", reparsed)
    check("json output truncated", "TRUNCATED" in reparsed["output"] and len(reparsed["output"]) < 5000, "")

    # 5. big JSON list (search_files shape) -> left alone
    listy = json.dumps({"matches": [{"file": f"f{i}", "line": "y" * 50} for i in range(500)], "total": 500})
    out, m = plug.cap_result("search_files", listy, "ok", POLICY)
    check("json list left alone", out is None and m["reason"] == "json-no-dominant-text-field", m)

    # 6. untargeted tool -> untouched
    out, m = plug.cap_result("skills_list", "z" * 5000, "ok", POLICY)
    check("untargeted untouched", out is None and m["reason"] == "untargeted", m)

    # 7. boundary: exactly threshold -> untouched
    _, m = plug.cap_result("read_file", "x" * 1000, "ok", POLICY)
    check("boundary == threshold untouched", m["reason"] == "under-threshold", m)

    # 8. non-string result -> untouched
    out, m = plug.cap_result("read_file", {"a": 1}, "ok", POLICY)
    check("non-string untouched", out is None and m["reason"] == "non-string", m)

    # 9. head+tail would cover everything -> untouched (guard)
    p2 = dict(POLICY, threshold_chars=100, head_chars=490, tail_chars=490)
    _, m = plug.cap_result("read_file", "x" * 500, "ok", p2)
    check("head+tail covers all -> untouched", m["action"] == "skip", m)

    # 10. dry_run mode: handler decides but does NOT modify, yet logs
    log = "/tmp/tool-output-cap-test.jsonl"
    if os.path.exists(log):
        os.remove(log)
    plug._POLICY_CACHE = dict(POLICY, mode="dry_run")
    r = plug._on_transform_tool_result(tool_name="read_file", result="q" * 5000, status="ok")
    check("dry_run returns None", r is None, r)
    check("dry_run logged", os.path.exists(log) and '"mode": "dry_run"' in open(log).read(), "")
    plug._POLICY_CACHE = None

    # 11. live mode: handler returns capped
    plug._POLICY_CACHE = dict(POLICY, mode="on")
    r = plug._on_transform_tool_result(tool_name="read_file", result="q" * 5000, status="ok")
    check("on mode returns capped", isinstance(r, str) and "TRUNCATED" in r and len(r) < 5000, "")
    plug._POLICY_CACHE = None

    # 12. handler fails open on garbage input
    r = plug._on_transform_tool_result(tool_name="read_file", result=None, status="ok")
    check("handler None-safe", r is None, r)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()