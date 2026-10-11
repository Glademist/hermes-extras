#!/usr/bin/env python3
"""Re-run dreamer on last-week sessions with gemma_local backend, into sandbox.

Usage: LLM_BACKEND=gemma_local HERMES_NOEMEM_HOME=<sandbox> python3 run_gemma_dreamer.py
Reads /tmp/gemma_session_manifest.json → for each session_id runs:
    dreamer_v2.py --session-id <id>  (writes archive/state into sandbox home)
"""
import json
import os
import subprocess
import sys
import time

SCRIPT_DIR = "~/hermes-noemem/runtime/scripts"
SB = os.environ.get("HERMES_NOEMEM_HOME", "~/hermes-noemem-sandbox-gemma")

manifest = json.load(open("/tmp/gemma_session_manifest.json"))
ids = manifest["session_ids"]
print(f"Re-running dreamer (gemma_local) on {len(ids)} sessions → sandbox {SB}", flush=True)

ok = []
fail = []
for i, sid in enumerate(ids, 1):
    print(f"\n[{i}/{len(ids)}] {sid} ...", flush=True)
    t0 = time.time()
    env = dict(os.environ)
    env["LLM_BACKEND"] = "gemma_local"
    env["HERMES_NOEMEM_HOME"] = SB
    env["HERMES_SESSIONS_DIR"] = f"{SB}/sessions"
    r = subprocess.run(
        [sys.executable, "-u", f"{SCRIPT_DIR}/dreamer_v2.py", "--session-id", sid],
        capture_output=True, text=True, timeout=1800, env=env,
    )
    dt = time.time() - t0
    tail = (r.stdout or "").strip().splitlines()
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
json.dump({"ok": ok, "fail": fail}, open("/tmp/gemma_dreamer_result.json", "w"), indent=2)
