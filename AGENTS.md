# AGENTS.md — agent entry point

This repo contains three independent Hermes add-ons. Do not read everything; go
straight to the component the user asked about.

## Routing

- User wants a **truthful context/token footer** for Hermes replies →
  read `context-advisor/AGENTS.md`.
- User wants **tool results to consume less context** (so a long session grows
  more slowly) → read `tool-output-cap/AGENTS.md`.
- User wants **long-term memory** (session archiving, topic memory files,
  retrieval) → read `noemem/AGENTS.md`.

## Rules

- Each component is self-contained. Do not assume the others are installed.
- Install = copy the plugin directory into `~/.hermes/plugins/<name>/`, add the
  name to `plugins.enabled` in `~/.hermes/config.yaml`, and restart the gateway.
  `noemem` is not a plugin — it is scheduled scripts; its AGENTS.md covers that.
- Verify with the component's own verification command *before* reporting success.
  Do not claim an install worked because the copy succeeded — run the test.
- These are the original author's tools, de-personalised. Paths use `~`; adapt them.
- Nothing here requires secrets. If a step seems to ask for a credential, stop and
  ask the user.
