# Example — session → archive

A fabricated, minimal illustration of what the Dreamer produces. Nothing here is
real data; it is a shape reference.

## Input (a chat session)

`sessions.json` lists sessions; the content snapshot holds the messages. Schematically:

```json
{
  "session_id": "20260101_101500_ab12cd34",
  "updated_at": "2026-01-01T11:20:00",
  "messages": [
    {"role": "user",      "content": "Set up a nightly backup of the data dir to the mini PC over SSH."},
    {"role": "assistant", "content": "Done. Wrote ~/scripts/backup-nightly.sh, cron 03:00, restic to donkey:/backups."},
    {"role": "user",      "content": "Good, and log the run to ~/backups/backup.log."}
  ]
}
```

## Output (one archive record)

See [`archive-example.md`](archive-example.md). Note the properties the pipeline
enforces:

- A dated filename `{date}_{project}_{topic}.md`.
- Frontmatter with `project`, `category`, `importance`, `retrieval_tier`.
- An outcome that records **end state / durable handles** (path, host, schedule) —
  not the conversation's back-and-forth.
- An honest lesson field (may be `none`).

The project (`backups`) is assigned by `projekt_utils.resolve_project()` against the
catalog — not trusted blindly from the LLM.
