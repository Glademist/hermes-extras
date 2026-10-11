# AGENTS.md — tool-output-cap

## What this is

A one-file Hermes plugin (`plugin/__init__.py`, ~200 lines) that caps oversized
tool results (head + marker + tail) on the `transform_tool_result` hook, so long
sessions consume less context. It registers exactly one hook and fails open.

## Dependencies

- Hermes Agent with the `transform_tool_result` hook.
- Python 3.9+ (stdlib only: `json`, `os`, `time`).

## Install

1. Copy the plugin directory into Hermes' plugin path:

   ```bash
   cp -R plugin ~/.hermes/plugins/tool-output-cap
   ```

2. Add `"tool-output-cap"` to the `plugins.enabled` list in `~/.hermes/config.yaml`.
   Verify it is a real YAML list, not a JSON string:

   ```bash
   python3 -c "import yaml;print(yaml.safe_load(open('$HOME/.hermes/config.yaml'))['plugins']['enabled'])"
   ```

3. Restart the gateway (from an external shell — never from inside a gateway
   session): `hermes gateway restart`.

## Verify (do this — do not report success on the copy alone)

Run the bundled test — it exercises the pure decision function and both handlers,
and prints `ALL TESTS PASSED`:

```bash
python3 plugin/test_tool_output_cap.py
```

Then, after the gateway restarts, confirm live behaviour by reading the log after
a few large tool results:

```bash
tail -n 20 ~/.hermes/logs/tool-output-cap.jsonl
```

A healthy `dry_run` log shows records with `"action": "cap"` and an `orig_chars`
larger than `new_chars` — the decision is made, nothing modified. Switch
`config.json` `"mode"` to `"on"` to apply.

## Failure modes / notes

- **Plugin enabled but nothing logs.** The hook did not load — check the plugin
  name is correctly spelled in `plugins.enabled` and the gateway was actually
  restarted. Plugin code loads in-process; a config change needs a restart to run.
- **A result you expected to be capped is not.** Check the tool name is in
  `target_tools` (an empty list means "all tools"), the result is a string (not a
  dict/list), and it exceeds `threshold_chars`.
- **JSON results.** Only a dict with a single dominant text field is capped; JSON
  lists and scalars are deliberately left alone.
- Do not lower `threshold_chars` far below the head+tail sum — if `head + tail`
  covers the whole result the plugin correctly refuses to cap.
