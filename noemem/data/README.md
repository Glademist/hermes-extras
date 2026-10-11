# noemem data tree (skeleton)

This directory mirrors the layout the pipeline expects under `$HERMES_NOEMEM_HOME`.
Its **contents are gitignored** — only the empty skeleton ships.

- archive/       one Markdown record per session (episodic history)
- memory/        current-state topic files (every claim dated)
- personal/      the mission file; user-owned, agent never writes it
- projekty/      per-project "now" summaries
- logs/          run logs and telemetry (JSONL)
- runtime/state/  per-script resumable state (JSON)
- backups/       dated backups of memory files before rewrites
