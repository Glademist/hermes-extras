# noemem

A long-term memory pipeline for a **Hermes** agent. It reads the agent's chat
sessions on a schedule, writes a structured **archive** record per session, then
**consolidates** those into dated, current-state **memory** topic files, and makes
all of it retrievable in future sessions.

The full reasoning is in [`DESIGN.md`](DESIGN.md). Read that first — this file is
the operational summary.

## Pipeline

```
sessions.json  ──►  dreamer_v2.py        (LLM: session → archive/{date}_{project}_{topic}.md)
                      │
                      ├─ archive_verify.py / archive_lint.py   (pure code: lint → quarantine)
                      │
                      ├─ memory_consolidate.py  (LLM: archives → memory/*.md topic files)
                      ├─ memory_polish.py       (weekly: proposals, nothing auto-applied)
                      ├─ generate_operating_context.py   (deterministic orientation file)
                      ├─ audit_memory_dates.py  (state claims must carry a date)
                      └─ qmd update && qmd embed   (index for retrieval)
```

The nightly entry point is `runtime/scripts/dreamer-v2-nightly.sh`. The local git
repo commits each run as `dreaming: run <ts>`.

## Layers

- **archive/** — one record per session (episodic history, permanent).
- **memory/** — current state per topic; every state claim carries a date.
- **memory/verdicts.md** — one line per trial→outcome.
- **personal/** — the mission, user-owned; the agent never writes it.

See `DESIGN.md` §2.

## Layout

```
noemem/
├── DESIGN.md            # the architecture & why — read this
├── AGENTS.md            # install/verify instructions for an agent
├── runtime/scripts/     # the pipeline scripts
├── examples/            # one fabricated session → archive, to show the shape
└── data/                # YOUR data tree (contents gitignored)
```

## Where it stores data

Scripts locate the data tree through `HERMES_NOEMEM_HOME` (default
`~/hermes-noemem`). The tree has `archive/`, `memory/`, `personal/`, `projekty/`,
`logs/`, `runtime/state/`, `backups/`. `data/` in this repo is an empty skeleton.

## Requirements

- Python 3.9+.
- An LLM backend. The default (`LLM_BACKEND=nous`) is a cloud OpenAI-compatible
  endpoint; the scripts also have local-backend clients. You supply your own.
- [QMD](https://github.com/…) for indexing/retrieval (optional — the archiving
  half runs without it).
- Hermes Agent, for the session database and the memory-write hook.

## Status

Shared as a working reference, not a turnkey package. Paths and backends are the
author's choices; adapt them. See `AGENTS.md` for the honest list of what you must
change.
