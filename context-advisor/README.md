# context-advisor

A truthful context-pressure footer for Hermes Discord replies.

The stock footer reads `Ctx: 315K / 1M` — but nothing compresses at 1M. The agent's
compressor fires at **50%** (`compression.threshold`), a between-turn gateway safety net at
**85%**, and a hard **400-message** valve forces compression regardless of tokens. So the
*effective* budget is ~524K, and `315K` is 60% of it, not 32%.

This project measures pressure against the **effective** budget, using the **usage-anchored**
figure the compressor actually triggers on, with the message valve as a second axis. Near a
boundary it says so. See [`design.md`](design.md) for the full rationale and the code citations.

## Layout

- `advisor/` — pure core (`core.py`), renderer + config (`render.py`), anchored-figure reader
  (`session_db.py`). Stdlib-only; no Hermes imports except inside `session_db.py`.
- `tests/` — `test_model.py` (core+render), `test_plugin.py` (plugin handlers).
- `tools/probe.py` — runs the whole pipeline against the real session DB.
- `design.md` — problem, ground truth, model, pre-compress safety-net design, cache-cost analysis.

The Hermes-facing shim lives at `~/.hermes/plugins/context-advisor/` and imports `advisor.*`.

## Run

Use the Hermes venv (it has `yaml` and the Hermes modules):

```bash
PY=~/.hermes/hermes-agent/venv/bin/python

$PY tests/test_model.py      # 9 checks, pure core + render
$PY tests/test_plugin.py     # 6 checks, plugin handlers
$PY tools/probe.py           # live footers for the most recent real sessions
```

Example:

```
Předělat a sjednotit web pro děti      n= 214
    new: Ctx: 91% · Msg: 54% · ⚠ handoff + /compress recommended
```

`Ctx` is token pressure against the **effective** budget (50% of the window), `Msg` is the
message-hard-limit axis; both are the true integer percents (not bucketed).

## Enable (done 2026-10-07)

The shim is enabled. It was a three-part change: manifest (present), the name in
`plugins.enabled`, and a gateway restart. The swap removed `context-footer` in the same edit so
replies don't carry two footers:

```bash
cp ~/.hermes/config.yaml ~/.hermes/backups/config/config.yaml.pre-context-advisor.$(date +%Y%m%d-%H%M%S)
hermes config set plugins.enabled '["approval-announce","context-advisor", ...]'   # drop context-footer
# gateway restart applies on the next process
```

## Not built yet

The pre-compress safety net (handoff file + lossiness judgment) is **designed, not built** —
see `design.md` §6. The correct handoff injection point is inside the `/compress` commit, which
is a small **core** change, not a plugin.
