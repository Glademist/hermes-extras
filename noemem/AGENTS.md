# AGENTS.md — noemem

## What this is

A scheduled, script-based long-term memory pipeline for a Hermes agent. It is **not
a plugin** — it is a set of Python scripts run by a nightly wrapper, plus a matching
data tree. It turns chat sessions into a structured archive, consolidates them into
dated, current-state topic memory files, and indexes everything for retrieval.

Read `DESIGN.md` to understand *why* before changing anything.

## Dependencies

- Python 3.9+.
- An LLM endpoint (OpenAI-compatible) for the Dreamer and consolidation steps.
- Hermes Agent — provides the session DB (`sessions.json`, `state.db`) and the
  `memory-write-guard` hook that this pipeline's writers are whitelisted through.
- [QMD](https://github.com/…) — optional; needed only for retrieval/indexing.

## Two honest warnings before you install

1. **This tree is de-personalised, not portable-as-is.** Scripts default to
   `~/hermes-noemem` for data and reference specific backend client modules. You must
   set `HERMES_NOEMEM_HOME` and configure an LLM backend before anything runs. Some
   scripts are reference material rather than runnable without adaptation.
2. **Nothing here has to be run just to be read.** If the user only wants to learn
   the design, read `DESIGN.md` and stop.

## Setup

1. Choose a data home and point the scripts at it:

   ```bash
   export HERMES_NOEMEM_HOME="$HOME/hermes-noemem"
   ```

2. Create the data tree (the scripts expect these subdirectories):

   ```bash
   mkdir -p "$HERMES_NOEMEM_HOME"/{archive,memory,personal,projekty,logs,runtime/state,backups}
   ```

   A skeleton lives in `data/` — copy it if you prefer.

3. Configure an LLM backend. The scripts read `LLM_BACKEND` (default `nous`); the
   client modules (`nous_client.py`, `hermes_client.py`, …) show the expected shape.
   You must supply your own credentials — there are none in this repo.

4. Seed `personal/project_catalog.md` (project slugs + `Keywords:` lines). The
   project guard in `projekt_utils.py` reads it; with no catalog it resolves
   everything to `unclassified`.

## Verify

The pipeline has pure-code pieces you can run without an LLM:

```bash
python3 runtime/scripts/archive_lint.py --help     # linter
python3 runtime/scripts/audit_memory_dates.py --help
python3 runtime/scripts/noemem_selfcheck.py --help
```

`archive_lint.py` over a directory of archives is the fastest proof the tree is
wired correctly. Run the Dreamer on **one** session before scheduling it:

```bash
python3 runtime/scripts/dreamer_v2.py --session-id <sid>
```

and confirm exactly one new `archive/<date>_<project>_<topic>.md` appears.

## Failure modes / notes

- **`HERMES_NOEMEM_HOME` unset** → scripts operate on `~/hermes-noemem`. Verify the
  path exists before trusting any output.
- **A sandboxed run starts with EMPTY data.** With no `project_catalog.md` in the
  sandbox, the project guard resolves everything to `unclassified` — output that
  looks like a guard bug is usually a missing file.
- **`prompt_guard/`** is a separate injection-detection utility pulled in by the
  web-safety path; it is not needed for the core archiving loop.
- A prompt change to the Dreamer must patch **all three** prompt paths (see
  `DESIGN.md` §3.2) or part of the traffic keeps the old text.
- The `memory-write-guard` hook fails open by design; the daily self-check is the
  actual safety net. Do not treat "the hook is installed" as "writes are safe".
