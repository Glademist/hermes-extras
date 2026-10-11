#!/usr/bin/env python3
"""Re-run dreamer on last-week sessions with cloud deepseek (nous), into sandbox,
capturing raw reasoning to a log for post-hoc audit.

Usage: LLM_BACKEND=nous HERMES_NOEMEM_HOME=<sandbox> python3 run_cloud_dreamer.py
Reads /tmp/gemma_session_manifest.json → for each session_id runs:
    dreamer_v2.py --session-id <id>  (writes archive/state into sandbox home)
Writes per-session reasoning to $NOUS_REASONING_LOG (append).
"""
import json
import os
import subprocess
import sys
import time

SCRIPT_DIR = "~/hermes-noemem/runtime/scripts"
SB = os.environ.get("HERMES_NOEMEM_HOME", "~/hermes-noemem-sandbox-cloud")
REASONING_LOG = os.environ.get("NOUS_REASONING_LOG", "/tmp/deepseek_reasoning.log")

manifest = json.load(open("/tmp/gemma_session_manifest.json"))
ids = manifest["session_ids"]
print(f"Re-running dreamer (cloud deepseek) on {len(ids)} sessions → sandbox {SB}", flush=True)
print(f"Reasoning capture → {REASONING_LOG}", flush=True)
open(REASONING_LOG, "w").close()  # fresh

ok = []
fail = []
for i, sid in enumerate(ids, 1):
    print(f"\n[{i}/{len(ids)}] {sid} ...", flush=True)
    t0 = time.time()
    env = dict(os.environ)
    env["LLM_BACKEND"] = "nous"
    env["HERMES_NOEMEM_HOME"] = SB
    env["HERMES_SESSIONS_DIR"] = f"{SB}/sessions"
    env["NOUS_REASONING_LOG"] = REASONING_LOG
    env["HERMES_NOEMEM_MODEL"] = os.environ.get("HERMES_NOEMEM_MODEL", "deepseek/deepseek-v4-flash")
    r = subprocess.run(
        [sys.executable, "-u", f"{SCRIPT_DIR}/dreamer_v2.py", "--session-id", sid],
        capture_output=True, text=True, timeout=1800, env=env,
    )
    dt = time.time() - t0
    if r.returncode == 0:
        ok.append(sid)
        status = "OK"
    else:
        fail.append(sid)
        status = f"FAIL(rc={r.returncode})"
    print(f"  -> {status} in {dt:.0f}s", flush=True)
    if r.returncode != 0:
        print("  STDERR:", (r.stderr or "")[-500:], flush=True)

print(f"\n=== DONE: {len(ok)} ok, {len(fail)} fail ===")
if fail:
    print("FAILED:", fail)
json.dump({"ok": ok, "fail": fail}, open("/tmp/cloud_dreamer_result.json", "w"), indent=2)
