# AGENTS.md — context-advisor

## What this is

A truthful context-pressure footer for Hermes Discord replies. The stock footer
shows the raw window (`Ctx: 315K / 1M`) but nothing compresses at 1M — the agent
compressor fires at 50 %, a gateway safety net at 85 %, and a hard 400-message
valve forces compression regardless of tokens. This component measures pressure
against the **effective** budget and the message valve, and warns near a boundary.

Two parts:
- `advisor/` — the pure core (`core.py`), renderer + config (`render.py`), and an
  offline anchored-figure reader (`session_db.py`). Stdlib-only except `session_db.py`.
- `plugin/` — the Hermes-facing shim that wires hooks to the package.

## Dependencies

- Hermes Agent with the `post_api_request`, `transform_llm_output`, and
  `on_session_end` hooks.
- Python 3.9+. `session_db.py` additionally imports Hermes internals
  (`agent.*`, `hermes_state_registry`) and is used only by offline probes.

## Install

1. Put the project somewhere and copy the shim into Hermes' plugin path:

   ```bash
   cp -R plugin ~/.hermes/plugins/context-advisor
   ```

2. Point the shim at the package (it defaults to `~/AI/context-advisor`):

   ```bash
   export CONTEXT_ADVISOR_HOME="$HOME/path/to/hermes-extras/context-advisor"
   ```

   or edit `_PROJECT` in `plugin/__init__.py`.

3. Add `"context-advisor"` to `plugins.enabled` in `~/.hermes/config.yaml`. If you
   also have the older `context-footer` plugin enabled, remove it in the same edit —
   two footers will otherwise stack.

4. Restart the gateway (from an external shell: `hermes gateway restart`).

## Verify (do this — do not report success on the copy alone)

Run the pure tests (no Hermes needed):

```bash
PY=~/.hermes/hermes-agent/venv/bin/python   # or any python3 with pyyaml
$PY tests/test_model.py      # core + render
$PY tests/test_plugin.py     # plugin handlers
```

Then, after a restart, confirm the footer is *computed*, not constant, by reading
the plugin's one INFO line per turn:

```bash
grep 'context-advisor' ~/.hermes/logs/agent.log | tail -n 5
# context-advisor <sid>: Ctx=NN% Msg=NN% (prompt_tokens=… message_count=… window=… budget=…)
```

## Failure modes / notes

- **No footer appears.** The hook fires only for `platform == "discord"`; check the
  gateway restarted after the edit; check `plugins.enabled`. The plugin also skips
  live voice chats by design.
- **`transform_llm_output` replaces the text.** The handler returns
  `original + footer` and guards with `if footer in response_text` for idempotency.
- **The provider figure is used, not the session DB.** The anchored figure in
  `session_db.py` fails to match the reloaded transcript for *active* sessions (the
  very ones a footer is for) — the raw provider `prompt_tokens` is always available
  and needs no per-turn DB read. `session_db.py` stays for offline probes only.
- Code changes to `advisor/render.py` do not take effect until the gateway is
  restarted — the running process keeps the old module.
