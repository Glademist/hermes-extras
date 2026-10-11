#!/bin/bash
# Nightly dreamer v2 pipeline — skripty v repozitáři hermes-noemem (bez symlinků).
# Phase 2 (2026-07-10): verify → consolidate → qmd → git commit.
set -uo pipefail
# Default archival model: LOCAL Nex-N2.5-mini (:8096, oMLX) — cloud-independent.
# Reasoning: nex_local_client maps enable_thinking=True -> chat_template_kwargs
# reasoning_effort=medium (Nex ignores enable_thinking). Czech JSON verified 2026-09-14.
# FAILSAFE (unchanged): escape hatch to cloud deepseek — LLM_BACKEND=nous
#   HERMES_NOEMEM_MODEL=deepseek/deepseek-v4-flash
# Alternatives: hermes (current chat model), darkbloom (local gemma 26b), mtplx (:8085),
#   gemma_local (:8096 former production — only valid while the gemma service runs).
export LLM_BACKEND=nous
# 2026-09-21: editor-prompt rework — cloud model for nightly curation (the user's call).
# Local Nex stays available via LLM_BACKEND=nex_local.
export NEX_LOCAL_MODEL="${NEX_LOCAL_MODEL:-Nex-N2.5-mini-MLX-oQ4}"
SCRIPT_DIR="~/hermes-noemem/runtime/scripts"
export HERMES_NOEMEM_HOME="${HERMES_NOEMEM_HOME:-$HOME/hermes-noemem}"
cd "$SCRIPT_DIR" || exit 1

echo "🌙 Dreamer v2 — $(date)"
python3 -u dreamer_v2.py --new-only
DREAMER_EXIT=$?
echo "Dreamer v2: exit $DREAMER_EXIT"
echo ""
# Projektovy updater removed 2026-07-10 (obsolete — see plans/2026-07-10_anthropic-alignment.md)
# Canonical updater vypnuto 2026-07 — viz layout-and-migration-map.md

echo "🔍 Verify (lint → quarantine) — $(date)"
python3 -u archive_verify.py || true
echo ""

echo "🧠 Memory consolidate (organize) — $(date)"
# Runs at 03:00/13:30: the 03:00 run consolidates yesterday's archives.
# The 13:30 run picks up anything archived this morning via --date today.
python3 -u memory_consolidate.py || true
if [ "$(date +%H)" -ge 12 ]; then
    python3 -u memory_consolidate.py --date "$(date +%Y-%m-%d)" || true
fi
echo ""

# Weekly polish + lesson harvest: Sunday morning run only (dreaming: Polish)
if [ "$(date +%u)" -eq 7 ] && [ "$(date +%H)" -lt 12 ]; then
    echo "✨ Memory polish (weekly audit) — $(date)"
    python3 -u memory_polish.py || true
    echo ""
    echo "🎓 Lesson harvester (procedural memory queue) — $(date)"
    python3 -u lesson_harvester.py || true
    echo ""
fi

echo "🧭 Operating context rebuild — $(date)"
python3 -u generate_operating_context.py || true
echo ""

echo "🗓️ Memory date audit (undated state claims) — $(date)"
# Fail-open; report-only (bez --fix, značky ručně/nebo až po dohodě).
# Metriky → logs/memory-audit-YYYY-MM-DD.log + memory-audit-baseline.json (daily trend).
python3 -u audit_memory_dates.py || true
echo ""

echo "📈 Living current status (refresh_current_status) — $(date)"
python3 -c "
import sys
sys.path.insert(0, '~/hermes-noemem/runtime')
sys.path.insert(0, '$SCRIPT_DIR')
from dreamer_v2 import refresh_current_status
refresh_current_status(['teacher', 'hermes-noemem', 'xnotes', 'mtplx', 'mlx-vlm', 'qmd', 'hermes'])
" || true
echo ""

echo "📊 QMD index rebuild — $(date)"
qmd update 2>&1 && qmd embed 2>&1
QMD_EXIT=$?
echo "QMD rebuild: exit $QMD_EXIT"
echo ""

echo "📝 Git commit (dreaming versioning) — $(date)"
(cd "$HERMES_NOEMEM_HOME" && git add -A && git commit -qm "dreaming: run $(date +%Y-%m-%d_%H%M)" 2>/dev/null) || echo "(nothing to commit)"
echo ""

echo "✨ Done — $(date)"
if [ $DREAMER_EXIT -ne 0 ] || [ $QMD_EXIT -ne 0 ]; then
    exit 1
fi
exit 0
