# tool-output-cap

A Hermes plugin that **caps oversized tool results before they enter the
context**, so a long agent session grows more slowly.

It is inspired by [RTK](https://www.rtk-ai.app/) (a CLI proxy that shrinks
`terminal` output), but it is our own plugin and does **not** wrap the `rtk`
binary — it applies the same economy to the *other* big text producers:
`read_file`, `web_extract`, `execute_code`, `browser_exec`, `search_files`, and
MCP getters.

## Why

On a long session, tool results dominate the prompt (measured ~80 % of a 100 K-token
context). Querying a log file, reading a 50 KB source file, or fetching a web page
all dump raw text straight into the context — and once it is there it stays and is
re-billed on every subsequent turn.

## What it does

On the `transform_tool_result` hook — which fires *after* the tool and *before* the
result is persisted, so capping here does **not** invalidate the prompt cache — it
replaces an oversized result with `head + explicit marker + tail`.

## Safety contract (a cap must never hide success/failure)

- An **error** result is never touched — errors stay fully visible.
- Only the **middle** is truncated, keeping the head (status line) **and** the tail
  (key result/footer).
- Every truncation appends an explicit, unmissable `…[tool-output-cap] TRUNCATED…`
  marker telling the model the result is incomplete and how to re-run for the rest.
- Results under the threshold are untouched.
- JSON dicts: only the single dominant text field is truncated and the dict is
  re-serialised, so structural fields (`exit_code`, `success`, …) are preserved and
  the JSON stays valid. Anything else is left alone (fail open).
- Any exception → return `None` (pass through). The plugin never breaks the tool path.

## Modes

`config.json → "mode"`:

- `"dry_run"` (default): decide + log, never modify. Safe to enable first.
- `"on"`: apply the cap.

## Config

`config.json`:

```json
{
  "mode": "on",
  "threshold_chars": 24000,
  "head_chars": 12000,
  "tail_chars": 6000,
  "log_path": "~/.hermes/logs/tool-output-cap.jsonl",
  "target_tools": ["read_file", "web_extract", "execute_code", "terminal",
                   "browser_exec", "search_files", "mcp__qmd__get",
                   "mcp__qmd__multi_get", "mcp__leviathan__get"]
}
```

Every cap is appended to `log_path` as JSONL (one record per decision, in both
modes) so you can measure how much it saves before switching to `"on"`.

## Install

See [`AGENTS.md`](AGENTS.md). Short version: copy `plugin/` to
`~/.hermes/plugins/tool-output-cap/`, add `tool-output-cap` to `plugins.enabled`,
restart the gateway. Start in `dry_run` for a day, read the log, then switch to `on`.

## Test

```bash
python3 plugin/test_tool_output_cap.py     # 12 checks, exits non-zero on failure
```
